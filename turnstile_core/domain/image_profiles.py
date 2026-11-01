from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import (
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from .models import StrictModel

IMAGE_RESPONSE_SAFETY_BYTES = 16 * 1024 * 1024


class ImageGenerationLimits(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_request_bytes: int = Field(strict=True, ge=1, le=IMAGE_RESPONSE_SAFETY_BYTES)
    max_response_bytes: int = Field(strict=True, ge=1, le=IMAGE_RESPONSE_SAFETY_BYTES)
    output_reservation_tokens: int = Field(strict=True, ge=1, le=2**31 - 1)
    timeout_seconds: int = Field(strict=True, ge=1, le=240)

    @model_validator(mode="after")
    def reservation_fits_counter(self) -> Self:
        if self.max_request_bytes + self.output_reservation_tokens > 2**31 - 1:
            raise ValueError("Image reservation exceeds the protocol counter range")
        return self

    @property
    def burst_reservation_tokens(self) -> int:
        return self.max_request_bytes + self.output_reservation_tokens


# Its burst reservation equals the canonical parent policy's image burst cap (32768).
DEFAULT_IMAGE_GENERATION_LIMITS = ImageGenerationLimits(
    max_request_bytes=24576,
    max_response_bytes=16777216,
    output_reservation_tokens=8192,
    timeout_seconds=180,
)


class ImageGenerationConfiguration(ImageGenerationLimits):
    sizes: tuple[str, ...] = Field(default=(), max_length=32)
    qualities: tuple[str, ...] = Field(default=(), max_length=32)
    formats: tuple[Literal["png", "jpeg"], ...] = Field(min_length=1, max_length=2)
    default_size: str
    default_quality: str
    default_format: Literal["png", "jpeg"]
    images_per_request: Literal[1] = 1
    stream: Literal[False] = False
    max_prompt_characters: int = Field(strict=True, ge=1, le=IMAGE_RESPONSE_SAFETY_BYTES)

    @model_validator(mode="after")
    def defaults_belong_to_profile(self) -> Self:
        if any(
            len(values) != len(set(values))
            for values in (self.sizes, self.qualities, self.formats)
        ):
            raise ValueError("Image configuration options must be unique")
        if any(not option.strip() for option in (
            *self.sizes, *self.qualities, self.default_size, self.default_quality,
        )):
            raise ValueError("Image parameter defaults and policy values cannot be blank")
        if (
            (self.sizes and self.default_size not in self.sizes)
            or (self.qualities and self.default_quality not in self.qualities)
            or self.default_format not in self.formats
        ):
            raise ValueError("Image defaults must belong to the declared profile")
        return self

class ImageGenerationProfile(ImageGenerationLimits):
    id: str = Field(min_length=1)
    version: Literal[1, 2, 3, 4]
    display_name: str = Field(min_length=1)
    provider_kind: Literal["microsoft_foundry"]
    provider_model: str = Field(min_length=1)
    protocol: Literal["openai_images"]
    api_version: str = Field(min_length=1)
    backend_path: str = Field(pattern=r"^/")
    public_path: str = Field(pattern=r"^/")
    usage_format: Literal["text-input-image-output-v1"]
    sizes: tuple[str, ...] | None = None
    qualities: tuple[str, ...] | None = None
    formats: tuple[Literal["png", "jpeg"], ...] | None = None
    default_size: str | None = None
    default_quality: str | None = None
    default_format: Literal["png", "jpeg"] | None = None
    images_per_request: Literal[1] | None = None
    stream: Literal[False] | None = None
    max_prompt_characters: int | None = None

    @model_validator(mode="after")
    def preserve_legacy_parameter_rules(self) -> Self:
        legacy_fields = (
            set(ImageGenerationConfiguration.model_fields) - set(ImageGenerationLimits.model_fields)
        )
        if self.version == 4:
            if any(getattr(self, field) is not None for field in legacy_fields):
                raise ValueError("Image publications only configure budget and transport limits")
            return self
        ImageGenerationConfiguration.model_validate({
            field: getattr(self, field) for field in ImageGenerationConfiguration.model_fields
        })
        if self.version in (1, 2) and (
            not self.sizes
            or not self.qualities
            or any(re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", size) is None
                   for size in self.sizes)
            or any(re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", quality) is None
                   for quality in self.qualities)
        ):
            raise ValueError("Historical image profiles require their original parameter rules")
        return self

    @model_serializer(mode="wrap")
    def serialize_snapshot(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        values = cast(dict[str, Any], handler(self))
        if self.version == 4:
            for field in (
                set(ImageGenerationConfiguration.model_fields)
                - set(ImageGenerationLimits.model_fields)
            ):
                values.pop(field, None)
        return values

    @property
    def configuration(self) -> ImageGenerationLimits:
        configuration_type = (
            ImageGenerationLimits if self.version == 4 else ImageGenerationConfiguration
        )
        return configuration_type.model_validate(
            self.model_dump(include=set(configuration_type.model_fields))
        )

    @property
    def limits(self) -> ImageGenerationLimits:
        return ImageGenerationLimits.model_validate(
            self.model_dump(include=set(ImageGenerationLimits.model_fields))
        )


def _create_image_profile(
    configuration: ImageGenerationLimits, provider_model: str, version: Literal[2, 3, 4],
) -> ImageGenerationProfile:
    values = {
        **configuration.model_dump(mode="json"),
        "version": version,
        "display_name": "OpenAI Images text to image",
        "provider_kind": "microsoft_foundry",
        "provider_model": provider_model,
        "protocol": "openai_images",
        "api_version": "preview",
        "backend_path": "/openai/v1/images/generations",
        "public_path": "/images/generations",
        "usage_format": "text-input-image-output-v1",
    }
    digest = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ImageGenerationProfile.model_validate({"id": "sha256:" + digest, **values})


def create_image_profile(
    configuration: ImageGenerationLimits, provider_model: str,
) -> ImageGenerationProfile:
    limits = ImageGenerationLimits.model_validate(
        configuration.model_dump(include=set(ImageGenerationLimits.model_fields))
    )
    return _create_image_profile(limits, provider_model, 4)


@lru_cache(maxsize=1)
def _historical_profiles() -> tuple[ImageGenerationProfile, ...]:
    return tuple(
        ImageGenerationProfile.model_validate(value)
        for value in json.loads(Path(__file__).with_name("image_profiles_v1.json").read_text())
    )


def validate_image_profile(value: object) -> ImageGenerationProfile:
    if value is None:
        raise ValueError("The model needs an explicitly published image profile")
    profile = ImageGenerationProfile.model_validate(value)
    if profile.version in (2, 3, 4):
        expected = _create_image_profile(
            profile.configuration, profile.provider_model, profile.version,
        )
        if profile != expected:
            raise ValueError("The published image profile does not match its immutable version")
        return profile
    if profile not in _historical_profiles():
        raise ValueError("The published image profile does not match its immutable version")
    return profile