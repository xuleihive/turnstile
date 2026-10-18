# APIM policy expressions and SSE frame literals are intentionally kept on policy-shaped
# lines; wrapping them for Python's line limit makes generated XML harder to review.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from ..domain.budget_counting import BudgetPublicationContracts
from ..domain.control_plane import (
    COUNT_TOKENS_REJECTIONS,
        ApiFormat,
        AuthStrategy,
        GatewayBackendPoolConfig,
        GatewayBackendPoolMember,
        GatewayModelBinding,
        GatewayPublication,
        StreamingMode,
)
from ..domain.image_profiles import validate_image_profile
from ..domain.model_availability import ModelAvailabilityPublication
from ..domain.runtime_models import BrandKey
from .apim_application_tpm import compose_application_tpm_policy
from .apim_control_plane_contract import (
        BackendCircuitBreakerResource,
        BackendPoolMemberResource,
        BackendPoolResource,
        BackendResource,
        CompiledGatewayRelease,
        NamedValueResource,
        OAuthCredentialResource,
        OperationResource,
        PolicyCompilationError,
)
from .apim_image_policy import IMAGE_OPERATION_ID, image_request_validation
from .apim_model_availability import (
    AVAILABILITY_PROBE_PREFIX,
    compile_availability_operation,
    compile_protocol_fenced_probe,
)
from .apim_policy_components import (
    IMAGE_POLICY_VERSION,
    NATIVE_STREAM_REQUEST,
    validate_native_stream_policy,
)
from .apim_storage_routes import FixedRouteBinding, FixedRouteProgram, compile_fixed_route_program


def _chat_completions_policy(publication: GatewayPublication) -> str:
        aliases = [
                item.id
                for item in publication.desired_spec.discovery_models
                if item.api_format is ApiFormat.OPENAI_CHAT
        ]
        condition = "false" if not aliases else " || ".join(
                "(string)context.Variables[&quot;dynamicChatModel&quot;] == "
                f"&quot;{escape(alias.casefold())}&quot;"
                for alias in aliases
        )
        return f"""<policies>
    <inbound>
        <set-variable name="dynamicChatModel" value="@{{
            try
            {{
                var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
                return ((string)body[&quot;model&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
            }}
            catch (Exception) {{ return &quot;&quot;; }}
        }}" />
        <set-variable name="selectedModelKnown" value="@({condition})" />
        <base />
    </inbound>
    <backend><base /></backend>
    <outbound><base /></outbound>
    <on-error><base /></on-error>
</policies>"""


class ApimPolicyCompiler:
    def __init__(
        self,
        probe_subscription_id: str = "turnstile-publisher-probe",
        usage_observer_url: str | None = None,
        usage_observer_key_named_value: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,126}", probe_subscription_id):
            raise ValueError("Invalid APIM probe subscription ID")
        self._probe_subscription_id = probe_subscription_id
        observer_url = (usage_observer_url or "").strip().rstrip("/")
        observer_key = (usage_observer_key_named_value or "").strip()
        if bool(observer_url) != bool(observer_key):
            raise ValueError("Usage observer URL and key Named Value must be configured together")
        if observer_url:
            parsed = urlsplit(observer_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
            ):
                raise ValueError("Usage observer URL must be an HTTPS origin")
        if observer_key and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}", observer_key):
            raise ValueError("Usage observer key Named Value is invalid")
        self._usage_observer_url = observer_url or None
        self._usage_observer_key_named_value = observer_key or None

    def without_usage_observer(self) -> ApimPolicyCompiler:
        return ApimPolicyCompiler(self._probe_subscription_id)

    def with_usage_observer(
        self, url: str | None, key_named_value: str | None,
    ) -> ApimPolicyCompiler:
        return ApimPolicyCompiler(self._probe_subscription_id, url, key_named_value)

    def with_probe_subscription(self, probe_subscription_id: str) -> ApimPolicyCompiler:
        return ApimPolicyCompiler(
            probe_subscription_id,
            self._usage_observer_url,
            self._usage_observer_key_named_value,
        )

    def compile_fixed_program(
        self, baseline: str, binding: FixedRouteBinding, *, canonical_parent: str,
    ) -> FixedRouteProgram:
        if binding.observer_key_named_value != self._usage_observer_key_named_value:
            raise PolicyCompilationError("Fixed program Observer binding differs from compiler")
        return compile_fixed_route_program(baseline, binding, canonical_parent=canonical_parent)

    def compile(self, publication: GatewayPublication) -> CompiledGatewayRelease:
        native_stream = self._native_stream_contract(publication)
        if publication.resource_manifest.get("budget_contracts") is not None:
            BudgetPublicationContracts.model_validate(
                publication.resource_manifest["budget_contracts"],
            ).validate_publication(publication)
        bindings = publication.desired_spec.bindings
        backend_ids: dict[str, str] = {}
        pool_member_backend_ids: dict[str, tuple[str, ...]] = {}
        target_backends: dict[tuple[str, str, str], str] = {}
        backends: list[BackendResource] = []
        named_values: dict[str, NamedValueResource] = {}
        oauth_credentials: dict[str, OAuthCredentialResource] = {}
        for binding in bindings:
            if not binding.routing_managed:
                continue
            if binding.backend_url is None:
                raise PolicyCompilationError("A managed runtime is missing its backend URL")
            pool = binding.backend_pool
            if binding.oauth is not None:
                if pool is not None:
                    raise PolicyCompilationError("OAuth connection pools are not supported")
                resource = OAuthCredentialResource(binding.oauth)
                existing = oauth_credentials.get(binding.oauth.provider_id)
                if existing is not None and existing != resource:
                    raise PolicyCompilationError("OAuth credential identity is inconsistent")
                oauth_credentials[binding.oauth.provider_id] = resource
            if binding.api_format is ApiFormat.OPENAI_IMAGES and pool is not None:
                raise PolicyCompilationError("Image generation pools are not supported")
            if pool is not None:
                member_resources: list[BackendPoolMemberResource] = []
                member_backend_ids: list[str] = []
                breaker = BackendCircuitBreakerResource(
                    failure_count=pool.rate_limit.circuit_breaker.failure_count,
                    interval_seconds=pool.rate_limit.circuit_breaker.interval_seconds,
                    trip_duration_seconds=(
                        pool.rate_limit.circuit_breaker.trip_duration_seconds
                    ),
                    accept_retry_after=(
                        pool.rate_limit.circuit_breaker.accept_retry_after
                    ),
                    status_code_ranges=tuple(
                        (item.minimum, item.maximum)
                        for item in pool.rate_limit.circuit_breaker.status_code_ranges
                    ),
                    error_reasons=pool.rate_limit.circuit_breaker.error_reasons,
                )
                for member in pool.members:
                    member_url = str(member.backend_url).rstrip("/")
                    member_upstream = self._validated_backend_url(member_url)
                    member_backend_id = self.pool_member_backend_id(
                        publication,
                        binding,
                        member,
                    )
                    backend_url, backend_headers = self._observer_backend(
                        member_url,
                        member_upstream,
                        pool_runtime_name=member.runtime_name,
                    )
                    member_auth_headers = self._pool_member_auth_headers(
                        binding,
                        member,
                    )
                    backends.append(
                        BackendResource(
                            id=member_backend_id,
                            title=f"FinOps {binding.model.display_name} · {member.runtime_name}",
                            url=backend_url,
                            headers=backend_headers + member_auth_headers,
                            circuit_breaker=breaker,
                        )
                    )
                    if member.named_value_name:
                        named_values[member.named_value_name] = NamedValueResource(
                            id=member.named_value_name,
                            key_vault_secret_id=None,
                        )
                    member_backend_ids.append(member_backend_id)
                    member_resources.append(
                        BackendPoolMemberResource(
                            backend_id=member_backend_id,
                            priority=member.priority,
                            weight=member.weight,
                        )
                    )
                pool_digest = hashlib.sha256(
                    json.dumps(
                        {
                            "pool": pool.model_dump(
                                mode="json",
                                exclude=set() if pool.session_affinity else {"session_affinity"},
                            ),
                            "member_backend_ids": member_backend_ids,
                            "apim_priority_contract": "one-based-v1",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:24 if pool.session_affinity else 10]
                pool_backend_id = (
                    f"turnstile-pool-affinity-{pool_digest}"
                    if pool.session_affinity else
                    f"turnstile-pool-{publication.id.hex[:12]}-{pool_digest}"
                )
                backends.append(
                    BackendPoolResource(
                        id=pool_backend_id,
                        title=f"FinOps {binding.model.display_name} deployment pool",
                        url="",
                        members=tuple(member_resources),
                        session_cookie_name=(
                            "TurnstileAffinity-" + hashlib.sha256(
                                f"{publication.gateway_profile_id}:{binding.model.model_key}".encode()
                            ).hexdigest()[:16]
                            if pool.session_affinity else None
                        ),
                    )
                )
                backend_ids[binding.model.model_key] = pool_backend_id
                pool_member_backend_ids[binding.model.model_key] = tuple(
                    member_backend_ids
                )
                continue
            source_backend_url = str(binding.backend_url).rstrip("/")
            upstream = self._validated_backend_url(source_backend_url)
            image_backend = binding.api_format is ApiFormat.OPENAI_IMAGES
            target_key = (
                upstream.scheme.casefold(),
                upstream.netloc.casefold(),
                upstream.path.rstrip("/") + ("|images" if image_backend else ""),
            )
            backend_id = target_backends.get(target_key)
            if backend_id is None:
                backend_kind = "img" if image_backend else "obs" if self._usage_observer_url is not None else "dyn"
                target_digest = hashlib.sha256(
                    "\n".join(target_key).encode("utf-8")
                ).hexdigest()[:10]
                backend_id = (
                    f"turnstile-{backend_kind}-{publication.id.hex[:12]}-{target_digest}"
                )
                target_backends[target_key] = backend_id
                backend_url, backend_headers = (
                    (source_backend_url, ()) if image_backend else
                    self._observer_backend(source_backend_url, upstream)
                )
                backends.append(
                    BackendResource(
                        id=backend_id,
                        title=f"FinOps {binding.runtime_name}",
                        url=backend_url,
                        headers=backend_headers,
                    )
                )
            backend_ids[binding.model.model_key] = backend_id
            if binding.named_value_name:
                named_values[binding.named_value_name] = NamedValueResource(
                    id=binding.named_value_name,
                    key_vault_secret_id=(
                        str(binding.key_vault_secret_id)
                        if binding.key_vault_secret_id is not None
                        else None
                    ),
                    owner_publication_id=str(publication.id),
                )

        chat_completions = self._chat_completions_policy(
            publication,
            backend_ids,
            pool_member_backend_ids,
        )
        responses = self._responses_policy(
            publication,
            backend_ids,
            pool_member_backend_ids,
        )
        responses_compact = self._responses_policy(
            publication,
            backend_ids,
            pool_member_backend_ids,
            compact=True,
        )
        messages = self._messages_policy(
            publication,
            backend_ids,
            pool_member_backend_ids,
        )
        count_tokens = self._count_tokens_policy(
            publication,
            backend_ids,
            pool_member_backend_ids,
        )
        models = self._models_policy(publication)
        includes_images = any(binding.api_format is ApiFormat.OPENAI_IMAGES for binding in bindings)
        includes_images = includes_images or any(
            removed.api_format is ApiFormat.OPENAI_IMAGES
            for removed in publication.desired_spec.removed_models
        )
        image_policy = self._images_policy(publication, backend_ids) if includes_images else None
        availability = publication.resource_manifest.get("model_availability")
        operations = [OperationResource(
            IMAGE_OPERATION_ID, "Image generations", "POST", "/images/generations",
        )] if image_policy is not None else []
        probe_policies: list[tuple[str, str]] = []
        if availability is not None:
            snapshot = ModelAvailabilityPublication.model_validate(availability)
            snapshot.validate_publication(publication)
            chat_completions, responses, responses_compact, messages, count_tokens = (
                compile_availability_operation(policy, publication, snapshot)
                for policy in (chat_completions, responses, responses_compact, messages, count_tokens)
            )
            models = compile_availability_operation(models, publication, snapshot, catalog=True)
            if image_policy is not None:
                image_policy = compile_availability_operation(image_policy, publication, snapshot)
            diagnostic_surfaces = [
                ("chat-completions", "/chat/completions", chat_completions),
                ("responses", "/responses", responses),
                ("responses-compact", "/responses/compact", responses_compact),
                ("anthropic-messages", "/v1/messages", messages),
                ("anthropic-count-tokens", "/v1/messages/count_tokens", count_tokens),
                ("anthropic-models", "/v1/models", models),
            ]
            if image_policy is not None:
                diagnostic_surfaces.append((IMAGE_OPERATION_ID, "/images/generations", image_policy))
            for operation_id, path, source in diagnostic_surfaces:
                probe_id = AVAILABILITY_PROBE_PREFIX + operation_id
                operations.append(OperationResource(probe_id, "Availability " + operation_id, "HEAD", path))
                probe_policies.append((probe_id, compile_protocol_fenced_probe(source)))
        for value in (
            chat_completions,
            responses,
            responses_compact,
            messages,
            count_tokens,
            models,
            *([image_policy] if image_policy is not None else []),
        ):
            ElementTree.fromstring(value)
        for operation_id, policy in (
            ("chat-completions", chat_completions),
            ("responses", responses),
            ("anthropic-messages", messages),
        ):
            if native_stream:
                validate_native_stream_policy(policy, operation_id)
        digest = hashlib.sha256(
            "\n".join(
                (
                    chat_completions,
                    responses,
                    responses_compact,
                    messages,
                    count_tokens,
                    models,
                    *([image_policy] if image_policy is not None else []),
                    *(source for _, source in probe_policies),
                )
            ).encode("utf-8")
        ).hexdigest()
        return CompiledGatewayRelease(
            chat_completions_policy=chat_completions,
            responses_policy=responses,
            responses_compact_policy=responses_compact,
            messages_policy=messages,
            count_tokens_policy=count_tokens,
            models_policy=models,
            policy_sha256=digest,
            backends=tuple(backends),
            named_values=tuple(named_values.values()),
            oauth_credentials=tuple(oauth_credentials.values()),
            images_generations_policy=image_policy,
            operations=tuple(operations),
            availability_probe_policies=tuple(probe_policies),
        )

    @staticmethod
    def _validated_backend_url(value: str) -> Any:
        upstream = urlsplit(value)
        if (
            upstream.scheme != "https"
            or not upstream.hostname
            or upstream.username is not None
            or upstream.password is not None
            or upstream.query
            or upstream.fragment
        ):
            raise PolicyCompilationError(
                "A managed runtime must use an HTTPS backend URL without credentials, "
                "query, or fragment"
            )
        return upstream

    def _observer_backend(
        self,
        source_backend_url: str,
        upstream: Any,
        *,
        pool_runtime_name: str | None = None,
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        if self._usage_observer_url is None:
            return source_backend_url, ()
        headers = [
            ("x-adapter-key", f"{{{{{self._usage_observer_key_named_value}}}}}"),
            ("x-turnstile-upstream-host", upstream.netloc),
        ]
        if pool_runtime_name is not None:
            headers.append(("x-hive-runtime", pool_runtime_name))
            headers.append(("x-turnstile-pool-runtime", pool_runtime_name))
        return (
            self._usage_observer_url + upstream.path.rstrip("/"),
            tuple(headers),
        )

    def pool_member_backend_id(
        self,
        publication: GatewayPublication,
        binding: GatewayModelBinding,
        member: GatewayBackendPoolMember,
    ) -> str:
        source_url = str(member.backend_url).rstrip("/")
        upstream = self._validated_backend_url(source_url)
        pool = binding.backend_pool
        if pool is None or not pool.session_affinity:
            return self._pool_member_backend_id(publication, binding, member, upstream)
        backend_url, backend_headers = self._observer_backend(
            source_url, upstream, pool_runtime_name=member.runtime_name
        )
        identity = {
            "gateway": str(publication.gateway_profile_id),
            "model": binding.model.model_key,
            "runtime": str(member.runtime_id),
            "url": backend_url,
            "headers": backend_headers + self._pool_member_auth_headers(binding, member),
            "breaker": pool.rate_limit.circuit_breaker.model_dump(mode="json"),
        }
        return "turnstile-pool-member-affinity-" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]

    @staticmethod
    def _pool_member_backend_id(
        publication: GatewayPublication,
        binding: GatewayModelBinding,
        member: GatewayBackendPoolMember,
        upstream: Any | None = None,
    ) -> str:
        parsed = upstream or urlsplit(str(member.backend_url).rstrip("/"))
        member_digest = hashlib.sha256(
            "\n".join(
                (
                    binding.model.model_key.casefold(),
                    str(member.runtime_id),
                    parsed.scheme.casefold(),
                    parsed.netloc.casefold(),
                    parsed.path.rstrip("/"),
                    member.auth_strategy.value,
                    member.named_value_name or "",
                    "pool-runtime-attribution-v2",
                )
            ).encode("utf-8")
        ).hexdigest()[:10]
        return f"turnstile-pool-member-{publication.id.hex[:12]}-{member_digest}"

    @staticmethod
    def _pool_member_auth_headers(
        binding: GatewayModelBinding,
        member: GatewayBackendPoolMember,
    ) -> tuple[tuple[str, str], ...]:
        if member.auth_strategy in {AuthStrategy.NONE, AuthStrategy.MANAGED_IDENTITY}:
            return ()
        named_value = member.named_value_name or ""
        if member.auth_strategy is AuthStrategy.NAMED_VALUE_BEARER:
            return (("Authorization", f"Bearer {{{{{named_value}}}}}"),)
        if member.auth_strategy is AuthStrategy.NAMED_VALUE_API_KEY:
            header = (
                "x-api-key"
                if binding.provider_brand_key is BrandKey.MICROSOFT_FOUNDRY
                and binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
                else "api-key"
            )
            return ((header, f"{{{{{named_value}}}}}"),)
        raise PolicyCompilationError("The pool member authentication strategy is unsupported")

    def _chat_completions_policy(
        self,
        publication: GatewayPublication,
        backend_ids: dict[str, str],
        pool_member_backend_ids: dict[str, tuple[str, ...]],
    ) -> str:
        metadata_branches = "\n".join(
            self._metadata_branch(binding, "dynamicChatModel", publication)
            for binding in publication.desired_spec.bindings
            if binding.api_format is ApiFormat.OPENAI_CHAT
        )
        metadata_choose = (
            f"""    <choose>
{metadata_branches}
    </choose>
"""
            if metadata_branches
            else ""
        )
        routing_branches = "\n".join(
            self._routing_branch(
                binding,
                backend_ids[binding.model.model_key],
                "dynamicChatModel",
                probe_backend_ids=pool_member_backend_ids.get(
                    binding.model.model_key,
                    (),
                ),
            )
            for binding in publication.desired_spec.bindings
            if binding.api_format is ApiFormat.OPENAI_CHAT
            and binding.routing_managed
        )
        known_condition = self._condition(
            [
                item.id
                for item in publication.desired_spec.discovery_models
                if item.api_format is ApiFormat.OPENAI_CHAT
            ],
            "dynamicChatModel",
            publication.desired_spec.bindings,
        )
        return f"""<policies>
  <inbound>
    <set-variable name="dynamicChatModel" value="@{{
      try
      {{
        var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
        return ((string)body[&quot;model&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
      }}
      catch (Exception) {{ return &quot;&quot;; }}
    }}" />
{self._pool_probe_variable()}
    <set-variable name="selectedModelKnown" value="@({known_condition})" />
{metadata_choose.rstrip()}
    <base />
    <choose>
{routing_branches}
      <when condition="@({known_condition})" />
      <otherwise>
        <return-response>
          <set-status code="400" reason="Bad Request" />
          <set-body>{{"error":{{"type":"invalid_request_error","message":"The requested model is not published by this gateway revision"}}}}</set-body>
        </return-response>
      </otherwise>
    </choose>
  </inbound>
{self._backend_policy(publication.desired_spec.bindings, "dynamicChatModel", ApiFormat.OPENAI_CHAT, native_stream=self._native_stream_contract(publication))}
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>"""

    def _images_policy(
        self, publication: GatewayPublication, backend_ids: dict[str, str]
    ) -> str:
        bindings = [
            binding for binding in publication.desired_spec.bindings
            if binding.api_format is ApiFormat.OPENAI_IMAGES and binding.routing_managed
        ]
        known = self._condition(
            [binding.model.model_key for binding in bindings], "dynamicImageModel", bindings,
        )
        profiles = {
            binding.model.model_key: validate_image_profile(binding.model.image_profile)
            for binding in bindings
        }
        validation = "\n".join(
            f'<when condition="@({self._model_condition(binding, "dynamicImageModel")})">'
            + image_request_validation(profiles[binding.model.model_key]) + '</when>'
            for binding in bindings
        ) or '<when condition="@(false)" />'
        metadata = "\n".join(self._metadata_branch(binding, "dynamicImageModel", publication) for binding in bindings)
        routing = "\n".join(self._routing_branch(
            binding, backend_ids[binding.model.model_key], "dynamicImageModel",
            backend_path=profiles[binding.model.model_key].backend_path
            + "?api-version=" + profiles[binding.model.model_key].api_version,
        ) for binding in bindings)
        metadata_choose = f"<choose>\n{metadata}\n</choose>" if metadata else ""
        routing = routing or '      <when condition="@(false)" />'
        return f"""<policies>
    <inbound>
        <set-variable name="dynamicImageModel" value="@{{ try {{ return ((string)context.Request.Body.As&lt;JObject&gt;(preserveContent: true)[&quot;model&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant(); }} catch {{ return &quot;&quot;; }} }}" />
        <choose>{validation}<otherwise><return-response><set-status code="400" reason="Bad Request" /><set-body>{{"error":"image_model_not_published"}}</set-body></return-response></otherwise></choose>
        <set-variable name="selectedModelKnown" value="@({known})" />
    {metadata_choose}
        <base />
        <choose><when condition="@(context.Variables.GetValueOrDefault&lt;int&gt;(&quot;imageGenerationPolicyVersion&quot;, 0) != {IMAGE_POLICY_VERSION})">
            <return-response><set-status code="503" reason="Service Unavailable" /><set-body>{{"error":"image_budget_policy_unavailable"}}</set-body></return-response>
        </when></choose>
        <choose>
{routing}
            <otherwise><return-response><set-status code="400" reason="Bad Request" /><set-body>{{"error":{{"code":"image_model_not_published","message":"The image model is not published by this gateway revision"}}}}</set-body></return-response></otherwise>
        </choose>
    </inbound>
    <backend><forward-request timeout="{max((profile.timeout_seconds for profile in profiles.values()), default=1)}" buffer-response="true" /></backend>
    <outbound><base /><set-header name="Cache-Control" exists-action="override"><value>no-store</value></set-header></outbound>
    <on-error><base /></on-error>
</policies>"""

    def _messages_policy(
        self,
        publication: GatewayPublication,
        backend_ids: dict[str, str],
        pool_member_backend_ids: dict[str, tuple[str, ...]],
    ) -> str:
        metadata_branches = "\n".join(
            self._metadata_branch(binding, "dynamicRequestedModel", publication)
            for binding in publication.desired_spec.bindings
            if binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
        )
        metadata_choose = (
            f"""    <choose>
{metadata_branches}
    </choose>
"""
            if metadata_branches
            else ""
        )
        routing_branches = "\n".join(
            self._routing_branch(
                binding,
                backend_ids[binding.model.model_key],
                "dynamicRequestedModel",
                probe_backend_ids=pool_member_backend_ids.get(
                    binding.model.model_key,
                    (),
                ),
            )
            for binding in publication.desired_spec.bindings
            if binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
            and binding.routing_managed
        )
        buffered_aliases = [
            binding.model.model_key
            for binding in publication.desired_spec.bindings
            if binding.streaming_mode is StreamingMode.BUFFERED
            and binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
        ]
        known_condition = self._condition(
            [
                item.id
                for item in publication.desired_spec.discovery_models
                if item.api_format is ApiFormat.ANTHROPIC_MESSAGES
            ],
            "dynamicRequestedModel",
            publication.desired_spec.bindings,
        )
        buffered_condition = self._condition(
            buffered_aliases,
            "dynamicRequestedModel",
            publication.desired_spec.bindings,
        )
        return f"""<policies>
  <inbound>
    <set-variable name="dynamicRequestedModel" value="@{{
      try
      {{
        var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
        return ((string)body[&quot;model&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
      }}
      catch (Exception) {{ return &quot;&quot;; }}
    }}" />
    <set-variable name="dynamicRequestedStream" value="@{{
      try
      {{
        var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
        return (bool?)body[&quot;stream&quot;] ?? false;
      }}
      catch (Exception) {{ return false; }}
    }}" />
{self._pool_probe_variable()}
    <set-variable name="selectedModelKnown" value="@({known_condition})" />
{metadata_choose.rstrip()}
    <base />
    <choose>
{routing_branches}
      <when condition="@({known_condition})" />
            <otherwise>
                <return-response>
                    <set-status code="400" reason="Bad Request" />
                    <set-body>{{"type":"error","error":{{"type":"invalid_request_error","message":"The requested model is not published by this gateway revision"}}}}</set-body>
                </return-response>
            </otherwise>
    </choose>
  </inbound>
{self._backend_policy(publication.desired_spec.bindings, "dynamicRequestedModel", ApiFormat.ANTHROPIC_MESSAGES, native_stream=self._native_stream_contract(publication))}
  <outbound>
    <base />
    <choose>
      <when condition="@(({buffered_condition}) &amp;&amp; (bool)context.Variables[&quot;dynamicRequestedStream&quot;] &amp;&amp; context.Response.StatusCode &lt; 400)">
{self._buffered_sse_body()}
      </when>
    </choose>
  </outbound>
  <on-error><base /></on-error>
</policies>"""

    def _responses_policy(
        self,
        publication: GatewayPublication,
        backend_ids: dict[str, str],
        pool_member_backend_ids: dict[str, tuple[str, ...]],
        *,
        compact: bool = False,
    ) -> str:
        bindings = [
            (binding, responses_path)
            for binding in publication.desired_spec.bindings
            if (
                responses_path := self._responses_backend_path(
                    binding,
                    compact=compact,
                )
            )
            is not None
        ]
        metadata_branches = "\n".join(
            self._metadata_branch(binding, "dynamicResponsesModel", publication)
            for binding, _ in bindings
        )
        metadata_choose = (
            f"""    <choose>
{metadata_branches}
    </choose>
"""
            if metadata_branches
            else ""
        )
        routing_branches = "\n".join(
            self._routing_branch(
                binding,
                backend_ids[binding.model.model_key],
                "dynamicResponsesModel",
                backend_path=responses_path,
                observer_api_format="openai_responses",
                include_chat_stream_usage=False,
                probe_backend_ids=pool_member_backend_ids.get(
                    binding.model.model_key,
                    (),
                ),
            )
            for binding, responses_path in bindings
        )
        known_condition = self._condition(
            [binding.model.model_key for binding, _ in bindings],
            "dynamicResponsesModel",
            [binding for binding, _ in bindings],
        )
        return f"""<policies>
  <inbound>
    <set-variable name="dynamicResponsesModel" value="@{{
      try
      {{
        var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
        return ((string)body[&quot;model&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
      }}
      catch (Exception) {{ return &quot;&quot;; }}
    }}" />
{self._pool_probe_variable()}
    <set-variable name="selectedModelKnown" value="@({known_condition})" />
{metadata_choose.rstrip()}
    <base />
    <choose>
{routing_branches}
        <when condition="@({known_condition})" />
      <otherwise>
        <return-response>
          <set-status code="400" reason="Bad Request" />
          <set-body>{{"error":{{"type":"invalid_request_error","message":"The requested model does not support the Responses API on this gateway revision"}}}}</set-body>
        </return-response>
      </otherwise>
    </choose>
  </inbound>
{self._backend_policy([binding for binding, _ in bindings], "dynamicResponsesModel", native_stream=not compact and self._native_stream_contract(publication))}
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>"""

    def _metadata_branch(
        self,
        binding: GatewayModelBinding,
        model_variable: str,
        publication: GatewayPublication | None = None,
        *,
        force_buffering: bool = True,
    ) -> str:
        provider = escape(binding.provider_brand_key.value)
        runtime = escape(binding.runtime_name)
        routing_managed = "true" if binding.routing_managed else "false"
        requires_assignment = "false"
        if binding.model.assignment_required:
            requires_assignment = (
                "!(context.Api.IsCurrentRevision == false &amp;&amp; context.Subscription != null "
                f"&amp;&amp; context.Subscription.Id == &quot;{escape(self._probe_subscription_id)}&quot; "
                "&amp;&amp; context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, "
                "&quot;&quot;) == &quot;gateway-publication-probe&quot;)"
            )
        force_buffered = ""
        if force_buffering and binding.streaming_mode is StreamingMode.BUFFERED:
            force_buffered = """
        <set-body>@{
          var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
          body[&quot;stream&quot;] = false;
          return body.ToString(Newtonsoft.Json.Formatting.None);
        }</set-body>"""
        match_condition = self._model_condition(binding, model_variable)
        counter_metadata = self._budget_counter_metadata(binding)
        contract_metadata = ""
        if publication is not None and publication.resource_manifest.get("budget_contracts") is not None:
            snapshot = BudgetPublicationContracts.model_validate(publication.resource_manifest["budget_contracts"])
            contract = snapshot.model_contracts[binding.model.model_key]
            encoded = json.dumps({
                "version": snapshot.version, "epoch": str(snapshot.epoch),
                "gateway_profile_id": str(snapshot.gateway_profile_id),
                "model_key": binding.model.model_key, "contract": contract.model_dump(mode="json"),
            }, separators=(",", ":"))
            contract_metadata = "\n        " + ElementTree.tostring(ElementTree.Element(
                "set-variable", {"name": "selectedBudgetCountContract", "value": encoded},
            ), encoding="unicode")
        return f"""      <when condition="@({match_condition})">
        <set-header name="x-hive-runtime" exists-action="override"><value>{runtime}</value></set-header>
        <set-variable name="selectedProviderName" value="{provider}" />
        <set-variable name="selectedBudgetModelKey" value="{escape(binding.model.model_key)}" />{contract_metadata}
        <set-variable name="selectedBudgetObserver" value="@({str(self._usage_observer_url is not None).lower()})" />
        <set-variable name="selectedRuntimeName" value="{runtime}" />
        <set-variable name="selectedContextWindow" value="@((long){binding.model.context_window or 0})" />
        <set-variable name="selectedRoutingManaged" value="@({routing_managed})" />
            <set-variable name="selectedRequiresAssignment" value="@({requires_assignment})" />{force_buffered}{counter_metadata}
      </when>"""

    @staticmethod
    def _budget_counter_metadata(binding: GatewayModelBinding) -> str:
        if not binding.routing_managed or binding.backend_url is None or binding.backend_pool is not None:
            return ""
        if binding.auth_strategy not in {
            AuthStrategy.MANAGED_IDENTITY, AuthStrategy.NAMED_VALUE_API_KEY,
            AuthStrategy.NAMED_VALUE_BEARER, AuthStrategy.NONE,
        }:
            return ""
        header = (
            "x-api-key" if binding.provider_brand_key.value == "microsoft_foundry"
            and binding.api_format is ApiFormat.ANTHROPIC_MESSAGES else "api-key"
        )
        values = {
            "selectedBudgetCounterOrigin": str(binding.backend_url),
            "selectedBudgetUpstreamModel": binding.model.upstream_model_id,
            "selectedBudgetCounterAuth": binding.auth_strategy.value,
            "selectedBudgetCounterHeader": header,
            "selectedBudgetCounterAudience": binding.managed_identity_resource or "",
        }
        nodes = "".join(
            f'\n        <set-variable name="{name}" value="{escape(value)}" />'
            for name, value in values.items()
        )
        if binding.named_value_name:
            nodes += (
                '\n        <set-variable name="selectedBudgetCounterKey" value="{{'
                + escape(binding.named_value_name) + '}}" />'
            )
        return nodes

    def _routing_branch(
        self,
        binding: GatewayModelBinding,
        backend_id: str,
        model_variable: str,
        *,
        backend_path: str | None = None,
        observer_api_format: str | None = None,
        include_chat_stream_usage: bool = True,
        probe_backend_ids: tuple[str, ...] = (),
    ) -> str:
        upstream = escape(binding.model.upstream_model_id)
        path = escape(
            (backend_path or binding.backend_path).replace("{upstream_model_id}", upstream)
        )
        auth = self._auth_policy(binding)
        backend_host = ""
        if binding.backend_url is not None:
            backend_host = escape(urlsplit(str(binding.backend_url)).netloc)
        host_header = (
            f'''\n        <set-header name="Host" exists-action="override"><value>{backend_host}</value></set-header>'''
            if backend_host and self._usage_observer_url is None and binding.backend_pool is None
            else ""
        )
        provider_headers = ""
        if (
            binding.provider_brand_key.value in {"microsoft_foundry", "azure_databricks"}
            and binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
        ):
            provider_headers = """
        <set-header name="anthropic-version" exists-action="override"><value>2023-06-01</value></set-header>"""
        transform = ""
        if binding.provider_brand_key.value == "amazon_bedrock":
            transform = """
        <set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
        <set-body>@{
          var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
          body.Remove(&quot;model&quot;);
          body.Remove(&quot;stream&quot;);
          body.Remove(&quot;context_management&quot;);
          body[&quot;anthropic_version&quot;] = &quot;bedrock-2023-05-31&quot;;
          return body.ToString(Newtonsoft.Json.Formatting.None);
        }</set-body>"""
        elif (
            binding.api_format is ApiFormat.OPENAI_CHAT
            or binding.provider_brand_key.value in {"microsoft_foundry", "azure_databricks"}
        ):
                        stream_usage = ""
                        if binding.api_format is ApiFormat.OPENAI_CHAT and include_chat_stream_usage:
                                stream_usage = """
                    if ((bool?)body[&quot;stream&quot;] ?? false)
                    {
                        var streamOptions = body[&quot;stream_options&quot;] as JObject ?? new JObject();
                        streamOptions[&quot;include_usage&quot;] = true;
                        body[&quot;stream_options&quot;] = streamOptions;
                    }"""
                        transform = f"""
        <set-body>@{{
          var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
                    body[&quot;model&quot;] = &quot;{upstream}&quot;;{stream_usage}
          return body.ToString(Newtonsoft.Json.Formatting.None);
        }}</set-body>"""
        observer_headers = self._observer_headers(binding, observer_api_format)
        response_encoding = """
        <set-header name="Accept-Encoding" exists-action="override"><value>identity</value></set-header>"""
        backend_selection = self._backend_selection(backend_id, probe_backend_ids)
        match_condition = self._model_condition(binding, model_variable)
        return f"""      <when condition="@({match_condition})">
    {backend_selection}
        <rewrite-uri template="{path}" copy-unmatched-params="false" />
    {auth}{host_header}{provider_headers}{response_encoding}{observer_headers}{transform}
      </when>"""

    def _pool_probe_variable(self) -> str:
        return f"""    <set-variable name="selectedPoolProbeBackend" value="@(
      context.Subscription != null
      &amp;&amp; context.Subscription.Id == &quot;{escape(self._probe_subscription_id)}&quot;
      &amp;&amp; context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, &quot;&quot;) == &quot;gateway-publication-probe&quot;
        ? context.Request.Headers.GetValueOrDefault(&quot;x-turnstile-pool-member&quot;, &quot;&quot;)
        : &quot;&quot;)" />"""

    @staticmethod
    def _backend_selection(
        backend_id: str,
        probe_backend_ids: tuple[str, ...],
    ) -> str:
        if not probe_backend_ids:
            return f'        <set-backend-service backend-id="{escape(backend_id)}" />'
        branches = "\n".join(
            "        <when condition=\"@((string)context.Variables[&quot;"
            f"selectedPoolProbeBackend&quot;] == &quot;{escape(probe_id)}&quot;)\">\n"
            f'          <set-backend-service backend-id="{escape(probe_id)}" />\n'
            "        </when>"
            for probe_id in probe_backend_ids
        )
        return f"""        <choose>
{branches}
          <otherwise>
            <set-backend-service backend-id="{escape(backend_id)}" />
          </otherwise>
        </choose>"""

    @staticmethod
    def _native_stream_contract(publication: GatewayPublication) -> bool:
        version = publication.resource_manifest.get("native_stream_contract_version")
        if "native_stream_contract_version" not in publication.resource_manifest:
            return publication.policy_sha256 is None and publication.status.value == "queued"
        if type(version) is not int or version != 1:
            raise PolicyCompilationError("Unknown native stream contract version")
        return True

    @classmethod
    def _backend_policy(
        cls,
        bindings: Any,
        model_variable: str,
        api_format: ApiFormat | None = None,
        *,
        native_stream: bool = True,
    ) -> str:
        pooled: list[tuple[GatewayModelBinding, GatewayBackendPoolConfig]] = []
        for binding in bindings:
            if api_format is not None and binding.api_format is not api_format:
                continue
            pool = binding.backend_pool
            if pool is not None:
                pooled.append((binding, pool))
        if not pooled:
            if not native_stream:
                return "  <backend><base /></backend>"
            condition = escape(NATIVE_STREAM_REQUEST, {'"': "&quot;"})
            return f"""  <backend>
        <choose>
            <when condition="{condition}">
                <forward-request buffer-response="false" />
            </when>
            <otherwise><forward-request /></otherwise>
        </choose>
    </backend>"""
        branches = "\n".join(
            f"""      <when condition="@({cls._model_condition(binding, model_variable)})">
                <choose>
                    <when condition="@(context.Variables.ContainsKey(&quot;executionReceiptHeader&quot;))">
                        <forward-request timeout="{pool.rate_limit.backend_timeout_seconds}" buffer-request-body="true" buffer-response="false" />
                    </when>
                    <otherwise>
        <retry condition="@(context.Response != null &amp;&amp; context.Response.StatusCode == 429)"
          count="{pool.rate_limit.max_attempts_per_request - 1}"
          interval="{pool.rate_limit.retry_interval_seconds}"
          first-fast-retry="{str(pool.rate_limit.first_fast_retry).lower()}">
                    <forward-request timeout="{pool.rate_limit.backend_timeout_seconds}" buffer-request-body="true" buffer-response="false" />
        </retry>
                    </otherwise>
                </choose>
      </when>"""
            for binding, pool in pooled
        )
        return f"""  <backend>
    <choose>
{branches}
      <otherwise>
        <forward-request buffer-response="false" />
      </otherwise>
    </choose>
  </backend>"""

    def _observer_headers(
        self,
        binding: GatewayModelBinding,
        api_format: str | None = None,
    ) -> str:
        if self._usage_observer_url is None or binding.api_format is ApiFormat.OPENAI_IMAGES:
            return ""
        literal_headers = {
            "x-provider-name": binding.provider_brand_key.value,
            "x-hive-runtime": binding.runtime_name,
            "x-turnstile-api-format": api_format or binding.api_format.value,
        }
        versioned_headers = {
            "x-turnstile-usage-id": "telemetryUsageId",
            "x-turnstile-gateway-profile-id": "telemetryGatewayProfileId",
            "x-turnstile-native-gateway-id": "telemetryNativeGatewayId",
            "x-turnstile-native-gateway-instance-id": (
                "telemetryNativeGatewayInstanceId"
            ),
            "x-turnstile-apim-service-id": "telemetryApimServiceId",
            "x-turnstile-apim-region": "telemetryApimRegion",
            "x-turnstile-api-revision": "telemetryApiRevision",
        }
        variable_headers = {
            "x-request-id": "callerRequestId",
            "x-org-name": "telemetryOrganization",
            "x-org-id": "telemetryOrganizationId",
            "x-department-name": "telemetryDepartment",
            "x-department-id": "telemetryDepartmentId",
            "x-project-name": "telemetryProject",
            "x-project-id": "telemetryProjectId",
            "x-agent-name": "telemetryAgent",
            "x-agent-id": "telemetryAgentId",
            "x-user-name": "telemetryUser",
            "x-user-id": "telemetryUserId",
            "x-hive-workflow": "telemetryWorkflow",
            "x-hive-run-id": "telemetryRunId",
            "x-hive-turn-index": "telemetryTurnIndex",
            "x-hive-model": "telemetryModel",
            "x-model-id": "telemetryModelId",
            "x-request-source": "telemetryRequestSource",
        }
        lines = [
            '        <set-header name="x-turnstile-correlation-id" exists-action="override">'
            '<value>@(context.RequestId.ToString())</value></set-header>'
        ]
        lines.append(
            '        <choose><when condition="@(context.Variables.ContainsKey('
            '&quot;executionReceiptHeader&quot;))">'
        )
        lines.append(
            '        <set-header name="x-turnstile-budget-execution" exists-action="override">'
            '<value>@(context.Variables.GetValueOrDefault&lt;string&gt;('
            '&quot;executionReceiptHeader&quot;, &quot;&quot;))</value></set-header>'
        )
        for field, variable in (
            ("apim-subscription-id", "telemetryApimSubscriptionId"),
            ("application-actor-type", "telemetryApplicationActorType"),
            ("application-actor-id", "telemetryApplicationActorId"),
        ):
            lines.append(
                f'        <set-header name="x-turnstile-{field}" exists-action="override">'
                '<value>@(context.Variables.GetValueOrDefault&lt;string&gt;('
                f'&quot;{variable}&quot;, &quot;&quot;))</value></set-header>'
            )
        lines.append('        </when><otherwise>')
        for field in (
            "budget-execution", "apim-subscription-id", "application-actor-type", "application-actor-id",
        ):
            lines.append(
                f'        <set-header name="x-turnstile-{field}" exists-action="delete" />'
            )
        lines.append('        </otherwise></choose>')
        lines.extend(
            f'        <set-header name="{name}" exists-action="override">'
            f'<value>{escape(value)}</value></set-header>'
            for name, value in literal_headers.items()
        )
        lines.extend(
            f'        <set-header name="{name}" exists-action="override">'
            + (f'<value>@(context.Variables.GetValueOrDefault&lt;string&gt;('
               f'&quot;executionModelId&quot;, (string)context.Variables[&quot;{variable}&quot;]))'
               if variable == "telemetryModelId" else
               f'<value>@((string)context.Variables[&quot;{variable}&quot;])')
            + '</value></set-header>'
            for name, variable in variable_headers.items()
        )
        lines.append(
            '        <choose><when condition="@(context.Variables.ContainsKey('
            '&quot;telemetryUsageId&quot;))">'
            '<set-header name="x-turnstile-event-schema-version" exists-action="override">'
            '<value>2</value></set-header>'
        )
        lines.extend(
            f'        <set-header name="{name}" exists-action="override">'
            f'<value>@((string)context.Variables[&quot;{variable}&quot;])</value></set-header>'
            for name, variable in versioned_headers.items()
        )
        lines.append(
            '        <set-header name="x-turnstile-gateway-is-managed" '
            'exists-action="override"><value>@(((bool)context.Variables['
            '&quot;telemetryGatewayIsManaged&quot;]).ToString().ToLowerInvariant())'
            "</value></set-header>"
        )
        lines.append('        </when><otherwise>')
        lines.extend(
            f'        <set-header name="{name}" exists-action="delete" />'
            for name in (
                "x-turnstile-event-schema-version", *versioned_headers,
                "x-turnstile-gateway-is-managed",
            )
        )
        lines.append('        </otherwise></choose>')
        return "\n".join(lines) + "\n"

    @staticmethod
    def _responses_backend_path(
        binding: GatewayModelBinding,
        *,
        compact: bool = False,
    ) -> str | None:
        if (
            binding.api_format is ApiFormat.OPENAI_CHAT
            and binding.routing_managed
            and binding.backend_path.rstrip("/") == "/openai/v1/chat/completions"
        ):
            return (
                "/openai/v1/responses/compact"
                if compact
                else "/openai/v1/responses"
            )
        return None

    @staticmethod
    def _auth_policy(binding: GatewayModelBinding) -> str:
        oauth = binding.oauth
        if oauth is not None:
            return (
                f'        <get-authorization-context provider-id="{escape(oauth.provider_id)}" '
                f'authorization-id="{escape(oauth.authorization_id)}" '
                'context-variable-name="turnstileProviderAuthorization" '
                'identity-type="managed" ignore-error="false" />\n'
                '        <set-header name="Authorization" exists-action="override">\n'
                '          <value>@("Bearer " + ((Authorization)context.Variables['
                '"turnstileProviderAuthorization"]).AccessToken)</value>\n'
                '        </set-header>\n'
            )
        if binding.auth_strategy is AuthStrategy.NAMED_VALUE_BEARER:
            # Bedrock API keys use this bearer shape. Named Values keep the actual key out
            # of PostgreSQL, generated policy, Function logs and browser responses.
            return f"""        <set-header name="Authorization" exists-action="override">
          <value>Bearer {{{{{escape(binding.named_value_name or "")}}}}}</value>
        </set-header>
"""
        if binding.auth_strategy is AuthStrategy.MANAGED_IDENTITY:
                        return f"""        <authentication-managed-identity resource="{escape(binding.managed_identity_resource or "")}" output-token-variable-name="turnstileProviderAccessToken" ignore-error="false" />
                <set-header name="Authorization" exists-action="override">
                    <value>@("Bearer " + (string)context.Variables["turnstileProviderAccessToken"])</value>
                </set-header>
"""
        if binding.auth_strategy is AuthStrategy.NAMED_VALUE_API_KEY:
            header = (
                "x-api-key"
                if binding.provider_brand_key.value == "microsoft_foundry"
                and binding.api_format is ApiFormat.ANTHROPIC_MESSAGES
                else "api-key"
            )
            return f"""        <set-header name="{header}" exists-action="override">
          <value>{{{{{escape(binding.named_value_name or "")}}}}}</value>
        </set-header>
"""
        if binding.auth_strategy is AuthStrategy.NONE:
            return ""
        raise PolicyCompilationError("The selected authentication strategy is unsupported")

    def _count_tokens_policy(
        self,
        publication: GatewayPublication,
        backend_ids: dict[str, str],
        pool_member_backend_ids: dict[str, tuple[str, ...]],
    ) -> str:
        supported = [
            binding for binding in publication.desired_spec.bindings
            if binding.count_tokens_operation.support == "supported"
        ]
        known = [
            item.id
            for item in publication.desired_spec.discovery_models
            if item.api_format is ApiFormat.ANTHROPIC_MESSAGES
        ]
        supported_aliases = {binding.model.model_key.casefold() for binding in supported}
        unavailable_condition = self._condition(
            [alias for alias in known if alias.casefold() not in supported_aliases],
            "dynamicCountModel",
            publication.desired_spec.bindings,
        )
        known_condition = self._condition(
            known,
            "dynamicCountModel",
            publication.desired_spec.bindings,
        )
        metadata_branches = "\n".join(
            self._metadata_branch(
                binding, "dynamicCountModel", publication, force_buffering=False,
            ) for binding in supported
        )
        metadata_choose = (
            f"    <choose>\n{metadata_branches}\n    </choose>\n"
            if metadata_branches else ""
        )
        routing_branches = "\n".join(
            self._routing_branch(
                binding,
                backend_ids[binding.model.model_key],
                "dynamicCountModel",
                backend_path=binding.count_tokens_operation.backend_path,
                observer_api_format="anthropic_count_tokens",
                include_chat_stream_usage=False,
                probe_backend_ids=pool_member_backend_ids.get(binding.model.model_key, ()),
            ) for binding in supported
        )
        unavailable = COUNT_TOKENS_REJECTIONS["unavailable"]
        unpublished = COUNT_TOKENS_REJECTIONS["unpublished"]
        return f"""<policies>
  <inbound>
    <set-variable name="dynamicCountModel" value="@{{
      try
      {{
        var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
        return ((string)body[&quot;model&quot;] ?? &quot;&quot;).Trim().ToLowerInvariant();
      }}
      catch (Exception) {{ return &quot;&quot;; }}
    }}" />
        <set-variable name="selectedModelKnown" value="@({known_condition})" />
        <set-variable name="selectedRoutingManaged" value="@(true)" />
{self._pool_probe_variable()}
{metadata_choose}\
    <base />
    <choose>
            <when condition="@({unavailable_condition})">
        <return-response>
                    <set-status code="{unavailable.status_code}" reason="{unavailable.reason}" />
          <set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
                    <set-body>{escape(json.dumps(unavailable.payload(), separators=(",", ":")))}</set-body>
        </return-response>
      </when>
{routing_branches}
            <otherwise>
                <return-response>
                    <set-status code="{unpublished.status_code}" reason="{unpublished.reason}" />
                    <set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
                    <set-body>{escape(json.dumps(unpublished.payload(), separators=(",", ":")))}</set-body>
                </return-response>
            </otherwise>
    </choose>
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>"""

    @classmethod
    def _models_policy(cls, publication: GatewayPublication) -> str:
        binding_by_alias = {
            binding.model.model_key.casefold(): binding
            for binding in publication.desired_spec.bindings
        }
        scoped_gateway_ids = sorted(
            {
                deployment.native_gateway_id
                for binding in publication.desired_spec.bindings
                if binding.deployment_scope.mode.value == "selected"
                for deployment in binding.serving_deployments
            },
            key=str.casefold,
        )
        if not scoped_gateway_ids:
            return cls._models_payload_policy(publication.desired_spec.discovery_models)
        branches = "\n".join(
            f"""      <when condition="@({cls._gateway_id_condition(native_id)})">
{cls._models_return_response([
    item
    for item in publication.desired_spec.discovery_models
    if (
        (binding := binding_by_alias.get(item.id.casefold())) is None
        or binding.deployment_scope.mode.value == "all"
        or any(
            deployment.native_gateway_id.casefold() == native_id.casefold()
            for deployment in binding.serving_deployments
        )
    )
], indent="        ")}
      </when>"""
            for native_id in scoped_gateway_ids
        )
        fallback = [
            item
            for item in publication.desired_spec.discovery_models
            if (
                (binding := binding_by_alias.get(item.id.casefold())) is None
                or binding.deployment_scope.mode.value == "all"
            )
        ]
        return f"""<policies>
  <inbound>
    <base />
    <choose>
{branches}
      <otherwise>
{cls._models_return_response(fallback, indent="        ")}
      </otherwise>
    </choose>
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>"""

    @classmethod
    def _models_payload_policy(cls, discovery_models: list[Any]) -> str:
        return f"""<policies>
  <inbound>
    <base />
{cls._models_return_response(discovery_models, indent="    ")}
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>"""

    @staticmethod
    def _models_return_response(discovery_models: list[Any], *, indent: str) -> str:
        data = [
            {
                "type": "model",
                "id": item.id,
                "display_name": item.display_name,
                "created_at": "2026-01-01T00:00:00Z",
            }
            for item in discovery_models
        ]
        payload = json.dumps(
            {
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        nested = indent + "  "
        return f"""{indent}<return-response>
{nested}<set-status code="200" reason="OK" />
{nested}<set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
{nested}<set-body>{escape(payload)}</set-body>
{indent}</return-response>"""

    @classmethod
    def _condition(
        cls,
        aliases: list[str],
        variable: str,
        bindings: Any = (),
    ) -> str:
        if not aliases:
            return "false"
        binding_by_alias = {
            binding.model.model_key.casefold(): binding for binding in bindings
        }
        return " || ".join(
            cls._model_condition(
                binding_by_alias[alias.casefold()],
                variable,
                alias=alias.casefold(),
            )
            if alias.casefold() in binding_by_alias
            else f"(string)context.Variables[&quot;{variable}&quot;] == &quot;{escape(alias.casefold())}&quot;"
            for alias in aliases
        )

    @classmethod
    def _model_condition(
        cls,
        binding: GatewayModelBinding,
        variable: str,
        *,
        alias: str | None = None,
    ) -> str:
        alias_condition = (
            f"(string)context.Variables[&quot;{variable}&quot;] == "
            f"&quot;{escape(alias or binding.model.model_key)}&quot;"
        )
        if binding.deployment_scope.mode.value == "all":
            return alias_condition
        return f"({alias_condition} &amp;&amp; ({cls._gateway_scope_condition(binding)}))"

    @classmethod
    def _gateway_scope_condition(cls, binding: GatewayModelBinding) -> str:
        if binding.deployment_scope.mode.value == "all":
            return "true"
        native_ids = [
            deployment.native_gateway_id
            for deployment in binding.serving_deployments
        ]
        if not native_ids:
            raise PolicyCompilationError(
                "A selected Gateway scope has no immutable native Gateway IDs"
            )
        return " || ".join(cls._gateway_id_condition(item) for item in native_ids)

    @staticmethod
    def _gateway_id_condition(native_gateway_id: str) -> str:
        csharp_literal = json.dumps(native_gateway_id, ensure_ascii=True)[1:-1]
        xml_literal = escape(csharp_literal, {'"': "&quot;"})
        return (
            "string.Equals(context.Deployment.Gateway.Id, "
            f"&quot;{xml_literal}&quot;, "
            "StringComparison.OrdinalIgnoreCase)"
        )

    @staticmethod
    def _buffered_sse_body() -> str:
        return r"""        <set-header name="Content-Type" exists-action="override"><value>text/event-stream</value></set-header>
        <set-header name="Cache-Control" exists-action="override"><value>no-cache</value></set-header>
        <set-body>@{
          var message = context.Response.Body.As&lt;JObject&gt;(preserveContent: true);
          var frames = new List&lt;string&gt;();
          var content = message[&quot;content&quot;] as JArray ?? new JArray();
          var usage = message[&quot;usage&quot;] as JObject ?? new JObject();
          var requestedModel = (string)context.Variables[&quot;dynamicRequestedModel&quot;];
          var startMessage = new JObject(
            new JProperty(&quot;id&quot;, message[&quot;id&quot;] ?? new JValue(&quot;msg_apim_buffered&quot;)),
            new JProperty(&quot;type&quot;, &quot;message&quot;),
            new JProperty(&quot;role&quot;, &quot;assistant&quot;),
            new JProperty(&quot;model&quot;, requestedModel),
            new JProperty(&quot;content&quot;, new JArray()),
            new JProperty(&quot;stop_reason&quot;, JValue.CreateNull()),
            new JProperty(&quot;stop_sequence&quot;, JValue.CreateNull()),
            new JProperty(&quot;usage&quot;, new JObject(
              new JProperty(&quot;input_tokens&quot;, usage[&quot;input_tokens&quot;] ?? new JValue(0)),
              new JProperty(&quot;cache_creation_input_tokens&quot;, usage[&quot;cache_creation_input_tokens&quot;] ?? new JValue(0)),
              new JProperty(&quot;cache_read_input_tokens&quot;, usage[&quot;cache_read_input_tokens&quot;] ?? new JValue(0)),
              new JProperty(&quot;output_tokens&quot;, 0))));
          frames.Add(&quot;event: message_start\ndata: &quot; + new JObject(
            new JProperty(&quot;type&quot;, &quot;message_start&quot;),
            new JProperty(&quot;message&quot;, startMessage)
          ).ToString(Newtonsoft.Json.Formatting.None) + &quot;\n\n&quot;);
          var index = 0;
          foreach (var token in content)
          {
            var block = token as JObject;
            if (block == null) { continue; }
            var blockType = (string)block[&quot;type&quot;] ?? &quot;&quot;;
            var startBlock = blockType == &quot;text&quot;
              ? new JObject(new JProperty(&quot;type&quot;, &quot;text&quot;), new JProperty(&quot;text&quot;, &quot;&quot;))
              : blockType == &quot;tool_use&quot;
                ? new JObject(new JProperty(&quot;type&quot;, &quot;tool_use&quot;), new JProperty(&quot;id&quot;, block[&quot;id&quot;]), new JProperty(&quot;name&quot;, block[&quot;name&quot;]), new JProperty(&quot;input&quot;, new JObject()))
                : (JObject)block.DeepClone();
            frames.Add(&quot;event: content_block_start\ndata: &quot; + new JObject(
              new JProperty(&quot;type&quot;, &quot;content_block_start&quot;),
              new JProperty(&quot;index&quot;, index),
              new JProperty(&quot;content_block&quot;, startBlock)
            ).ToString(Newtonsoft.Json.Formatting.None) + &quot;\n\n&quot;);
            if (blockType == &quot;text&quot;)
            {
              frames.Add(&quot;event: content_block_delta\ndata: &quot; + new JObject(
                new JProperty(&quot;type&quot;, &quot;content_block_delta&quot;),
                new JProperty(&quot;index&quot;, index),
                new JProperty(&quot;delta&quot;, new JObject(new JProperty(&quot;type&quot;, &quot;text_delta&quot;), new JProperty(&quot;text&quot;, block[&quot;text&quot;] ?? new JValue(&quot;&quot;))))
              ).ToString(Newtonsoft.Json.Formatting.None) + &quot;\n\n&quot;);
            }
            else if (blockType == &quot;tool_use&quot;)
            {
              var input = block[&quot;input&quot;] == null ? &quot;{}&quot; : block[&quot;input&quot;].ToString(Newtonsoft.Json.Formatting.None);
              frames.Add(&quot;event: content_block_delta\ndata: &quot; + new JObject(
                new JProperty(&quot;type&quot;, &quot;content_block_delta&quot;),
                new JProperty(&quot;index&quot;, index),
                new JProperty(&quot;delta&quot;, new JObject(new JProperty(&quot;type&quot;, &quot;input_json_delta&quot;), new JProperty(&quot;partial_json&quot;, input)))
              ).ToString(Newtonsoft.Json.Formatting.None) + &quot;\n\n&quot;);
            }
            frames.Add(&quot;event: content_block_stop\ndata: &quot; + new JObject(
              new JProperty(&quot;type&quot;, &quot;content_block_stop&quot;), new JProperty(&quot;index&quot;, index)
            ).ToString(Newtonsoft.Json.Formatting.None) + &quot;\n\n&quot;);
            index++;
          }
          frames.Add(&quot;event: message_delta\ndata: &quot; + new JObject(
            new JProperty(&quot;type&quot;, &quot;message_delta&quot;),
            new JProperty(&quot;delta&quot;, new JObject(
              new JProperty(&quot;stop_reason&quot;, message[&quot;stop_reason&quot;] ?? new JValue(&quot;end_turn&quot;)),
              new JProperty(&quot;stop_sequence&quot;, message[&quot;stop_sequence&quot;] ?? JValue.CreateNull()))),
            new JProperty(&quot;usage&quot;, new JObject(new JProperty(&quot;output_tokens&quot;, usage[&quot;output_tokens&quot;] ?? new JValue(0))))
          ).ToString(Newtonsoft.Json.Formatting.None) + &quot;\n\n&quot;);
          frames.Add(&quot;event: message_stop\ndata: {\&quot;type\&quot;:\&quot;message_stop\&quot;}\n\n&quot;);
          return string.Concat(frames);
        }</set-body>"""

    def patch_parent_policy(self, source: str) -> str:
        source = source.replace("__LEGACY_PROVIDER_ROUTING__", "")
        source = source.replace("__USAGE_OBSERVER_HEADERS__", "")
        source = self._patch_legacy_provider_routing(source)
        source = self._patch_legacy_openai_stream_usage(source)
        compact_anchors = (
            'name="isResponsesCompactOperation"',
            'name="compactOutputBoundKnown"',
            '&quot;compact_budget_bound_unavailable&quot;',
            'body.Remove(&quot;client_metadata&quot;)',
        )
        if any(anchor not in source for anchor in compact_anchors):
            raise PolicyCompilationError(
                "The live parent policy does not contain governed Responses compact admission"
            )
        provider_neutral = (
            '<set-variable name="providerName" '
            'value="@(context.Variables.GetValueOrDefault&lt;string&gt;('
            '&quot;selectedProviderName&quot;, &quot;unattributed&quot;))" />'
        )
        provider_variants = (
            '<set-variable name="providerName" value="@((bool)context.Variables['
            '&quot;isAnthropicOperation&quot;] ? &quot;anthropic&quot; : '
            '&quot;microsoft_foundry&quot;)" />',
            '<set-variable name="providerName" '
            'value="@(context.Variables.GetValueOrDefault&lt;string&gt;('
            '&quot;selectedProviderName&quot;, (bool)context.Variables['
            '&quot;isAnthropicOperation&quot;] ? &quot;anthropic&quot; : '
            '&quot;microsoft_foundry&quot;))" />',
        )
        runtime_neutral = (
            '<value>@(context.Variables.GetValueOrDefault&lt;string&gt;('
            '&quot;selectedRuntimeName&quot;, &quot;unattributed&quot;))</value>'
        )
        runtime_variants = (
            '<value>@((bool)context.Variables[&quot;isAnthropicOperation&quot;] ? '
            '&quot;Azure Databricks Claude via APIM&quot; : '
            '&quot;Microsoft Foundry via APIM&quot;)</value>',
            '<value>@(context.Variables.GetValueOrDefault&lt;string&gt;('
            '&quot;selectedRuntimeName&quot;, (bool)context.Variables['
            '&quot;isAnthropicOperation&quot;] ? '
            '&quot;Azure Databricks Claude via APIM&quot; : '
            '&quot;Microsoft Foundry via APIM&quot;))</value>',
        )
        result = source
        if provider_neutral not in result and not any(
            variant in result for variant in provider_variants
        ):
            raise PolicyCompilationError(
                "The live parent policy no longer matches the provider metadata anchor"
            )
        if runtime_neutral not in result and not any(
            variant in result for variant in runtime_variants
        ):
            raise PolicyCompilationError(
                "The live parent policy no longer matches the runtime metadata anchor"
            )
        replacements = {
            "if (status != &quot;ok&quot;) { return status; }": (
                "var assignmentRequired = context.Variables.GetValueOrDefault&lt;bool&gt;("
                "&quot;selectedRequiresAssignment&quot;, false);\n"
                "              if (status != &quot;ok&quot;)\n"
                "              {\n"
                "                return status == &quot;policy_unconfigured&quot; "
                "&amp;&amp; assignmentRequired ? &quot;denied&quot; : status;\n"
                "              }"
            ),
        }
        for old, new in replacements.items():
            if new in result:
                continue
            if result.count(old) != 1:
                raise PolicyCompilationError(
                    "The live parent policy no longer matches the reviewed compiler baseline"
                )
            result = result.replace(old, new)

        guard = """        <choose>
                    <when condition="@((bool)context.Variables[&quot;isInferenceOperation&quot;] &amp;&amp; !context.Variables.GetValueOrDefault&lt;bool&gt;(&quot;selectedModelKnown&quot;, !(bool)context.Variables[&quot;isResponsesOperation&quot;]))">
            <return-response>
              <set-status code="400" reason="Bad Request" />
              <set-header name="Content-Type" exists-action="override">
                <value>application/json</value>
              </set-header>
              <set-body>{"type":"error","error":{"type":"invalid_request_error","message":"The requested model is not published by this gateway revision"}}</set-body>
            </return-response>
          </when>
        </choose>
"""
        guard_count = result.count("selectedModelKnown")
        if guard_count == 0:
            employee_pattern = re.compile(
                r'(<set-header name="x-request-source" exists-action="override">'
                r"\s*<value>employee-desktop</value>\s*</set-header>)"
            )
            employee_matches = list(employee_pattern.finditer(result))
            subscription_anchor = (
                "<!-- A subscription caller that omits the attribution headers still "
                "names its model in the"
            )
            if len(employee_matches) != 1 or result.count(subscription_anchor) != 1:
                raise PolicyCompilationError(
                    "The live parent policy no longer matches the reviewed auth anchors"
                )
            result = employee_pattern.sub(r"\1\n" + guard, result, count=1)
            result = result.replace(subscription_anchor, guard + subscription_anchor, 1)
        elif guard_count != 2:
            raise PolicyCompilationError(
                "The live parent policy contains a partial unknown-model guard"
            )
        result = self._patch_provider_header_hygiene(result)
        root = ElementTree.fromstring(result)
        if any(
            child.tag not in {"when", "otherwise"}
            for choose in root.iter("choose")
            for child in choose
        ):
            raise PolicyCompilationError(
                "The live parent policy contains an invalid direct child under choose"
            )
        return compose_application_tpm_policy(result)

    @staticmethod
    def _patch_legacy_openai_stream_usage(source: str) -> str:
        legacy_condition = (
            '<when condition="@(!(bool)context.Variables['
            '&quot;isAnthropicOperation&quot;])">'
        )
        responses_aware_condition = (
            '<when condition="@(!(bool)context.Variables['
            '&quot;isAnthropicOperation&quot;] &amp;&amp; '
            '!(bool)context.Variables[&quot;isResponsesOperation&quot;])">'
        )
        if responses_aware_condition in source:
            return source
        candidates = [
            index
            for index in range(len(source))
            if source.startswith(legacy_condition, index)
            and "stream_options" in source[index : index + 1200]
            and "include_usage" in source[index : index + 1200]
        ]
        if not candidates:
            return source
        if len(candidates) != 1:
            raise PolicyCompilationError(
                "The live parent policy has ambiguous legacy OpenAI stream usage"
            )
        index = candidates[0]
        return (
            source[:index]
            + responses_aware_condition
            + source[index + len(legacy_condition) :]
        )

    @staticmethod
    def _patch_legacy_provider_routing(source: str) -> str:
        legacy_pattern = re.compile(
            r'(?P<block><choose>\s*'
            r'<when condition="@\(\(bool\)context\.Variables\[&quot;isAnthropicOperation&quot;\]\)">'
            r'(?P<anthropic>.*?)</when>\s*'
            r'<otherwise>(?P<openai>.*?)</otherwise>\s*'
            r'</choose>)',
            re.DOTALL,
        )
        candidates = [
            match
            for match in legacy_pattern.finditer(source)
            if "set-backend-service" in match.group("anthropic")
            and "set-backend-service" in match.group("openai")
        ]
        if not candidates:
            return source
        if len(candidates) != 1:
            raise PolicyCompilationError(
                "The live parent policy has ambiguous legacy provider routing"
            )
        match = candidates[0]
        block = match.group("block")
        anthropic_condition = (
            '@(!context.Variables.GetValueOrDefault&lt;bool&gt;('
            '&quot;selectedRoutingManaged&quot;, false) &amp;&amp; '
            '(bool)context.Variables[&quot;isAnthropicOperation&quot;])'
        )
        unmanaged_condition = (
            '@(!context.Variables.GetValueOrDefault&lt;bool&gt;('
            '&quot;selectedRoutingManaged&quot;, false))'
        )
        guarded = block.replace(
            '@((bool)context.Variables[&quot;isAnthropicOperation&quot;])',
            anthropic_condition,
            1,
        )
        guarded = guarded.replace(
            "<otherwise>",
            f'<when condition="{unmanaged_condition}">',
            1,
        ).replace("</otherwise>", "</when>", 1)
        return source[: match.start()] + guarded + source[match.end() :]

    @staticmethod
    def _patch_provider_header_hygiene(source: str) -> str:
        snapshots = {
            "telemetryOrganization": 'context.Request.Headers.GetValueOrDefault(&quot;x-org-name&quot;, &quot;unattributed&quot;)',
            "telemetryOrganizationId": 'context.Request.Headers.GetValueOrDefault(&quot;x-org-id&quot;, &quot;unattributed&quot;)',
            "telemetryDepartment": 'context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;unattributed&quot;)',
            "telemetryDepartmentId": 'context.Request.Headers.GetValueOrDefault(&quot;x-department-id&quot;, &quot;unattributed&quot;)',
            "telemetryProject": 'context.Request.Headers.GetValueOrDefault(&quot;x-project-name&quot;, &quot;unattributed&quot;)',
            "telemetryProjectId": 'context.Request.Headers.GetValueOrDefault(&quot;x-project-id&quot;, &quot;unattributed&quot;)',
            "telemetryAgent": 'context.Request.Headers.GetValueOrDefault(&quot;x-agent-name&quot;, &quot;unattributed&quot;)',
            "telemetryAgentId": 'context.Request.Headers.GetValueOrDefault(&quot;x-agent-id&quot;, &quot;unattributed&quot;)',
            "telemetryUser": 'context.Request.Headers.GetValueOrDefault(&quot;x-user-name&quot;, &quot;unattributed&quot;)',
            "telemetryUserId": 'context.Request.Headers.GetValueOrDefault(&quot;x-user-id&quot;, &quot;unattributed&quot;)',
            "telemetryModelId": 'context.Request.Headers.GetValueOrDefault(&quot;x-model-id&quot;, &quot;unattributed&quot;)',
            "telemetryRequestSource": 'context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, &quot;unattributed&quot;)',
            "telemetryWorkflow": 'context.Request.Headers.GetValueOrDefault(&quot;x-hive-workflow&quot;, &quot;unattributed&quot;)',
            "telemetryRunId": 'context.Request.Headers.GetValueOrDefault(&quot;x-hive-run-id&quot;, (string)context.Variables[&quot;callerRequestId&quot;])',
            "telemetryTurnIndex": 'context.Request.Headers.GetValueOrDefault(&quot;x-hive-turn-index&quot;, &quot;1&quot;)',
            "telemetryModel": 'context.Request.Headers.GetValueOrDefault(&quot;x-hive-model&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-model-id&quot;, &quot;unattributed&quot;))',
            "telemetryRuntime": 'context.Request.Headers.GetValueOrDefault(&quot;x-hive-runtime&quot;, context.Variables.GetValueOrDefault&lt;string&gt;(&quot;selectedRuntimeName&quot;, &quot;unattributed&quot;))',
        }
        business_header_names = (
            "x-request-id",
            "x-org-id",
            "x-org-name",
            "x-department-id",
            "x-department-name",
            "x-project-id",
            "x-project-name",
            "x-agent-id",
            "x-agent-name",
            "x-model-id",
            "x-runtime-id",
            "x-user-id",
            "x-user-name",
            "x-request-source",
            "x-hive-organization",
            "x-hive-department",
            "x-hive-project",
            "x-hive-agent",
            "x-hive-user",
            "x-hive-workflow",
            "x-hive-run-id",
            "x-hive-turn-index",
            "x-hive-model",
            "x-hive-runtime",
            "x-turnstile-pool-member",
        )
        codex_header_names = (
            "originator",
            "session-id",
            "thread-id",
            "x-client-request-id",
            "x-codex-installation-id",
            "x-codex-routing-hint",
            "x-codex-turn-state",
            "x-codex-turn-metadata",
            "x-codex-parent-thread-id",
            "x-codex-window-id",
            "x-codex-beta-features",
            "x-openai-subagent",
            "x-openai-memgen-request",
            "x-responsesapi-include-timing-metrics",
            "x-openai-internal-codex-responses-lite",
            "x-oai-attestation",
            "OpenAI-Beta",
        )
        snapshot_lines = "".join(
            f'    <set-variable name="{name}" value="@({expression})" />\n'
            for name, expression in snapshots.items()
        )
        business_cleanup_lines = "".join(
            f'    <set-header name="{name}" exists-action="delete" />\n'
            for name in business_header_names
        )
        codex_cleanup_lines = (
            '    <choose>\n'
            '      <when condition="@((bool)context.Variables['
            '&quot;isResponsesOperation&quot;])">\n'
            + "".join(
                f'        <set-header name="{name}" exists-action="delete" />\n'
                for name in codex_header_names
            )
            + "      </when>\n"
            "    </choose>\n"
        )
        result = source
        core_marker = 'name="telemetryWorkflow"'
        complete_marker = 'name="telemetryOrganization"'
        if core_marker not in result:
            inbound_end = result.find("</inbound>")
            if inbound_end < 0:
                raise PolicyCompilationError("The live parent policy has no inbound boundary")
            result = (
                result[:inbound_end]
                + snapshot_lines
                + business_cleanup_lines
                + codex_cleanup_lines
                + result[inbound_end:]
            )
        elif complete_marker not in result:
            core_match = re.search(
                r'(?m)^(?P<indent>\s*)<set-variable name="telemetryWorkflow"',
                result,
            )
            cleanup_match = re.search(
                r'(?m)^(?P<indent>\s*)<set-header name="x-hive-organization" '
                r'exists-action="delete" />',
                result,
            )
            if core_match is None or cleanup_match is None:
                raise PolicyCompilationError(
                    "The live parent policy contains a partial provider-header boundary"
                )
            snapshot_indent = core_match.group("indent")
            cleanup_indent = cleanup_match.group("indent")
            business_snapshots = "".join(
                f'{snapshot_indent}<set-variable name="{name}" '
                f'value="@({expression})" />\n'
                for name, expression in list(snapshots.items())[:12]
            )
            business_cleanup = "".join(
                f'{cleanup_indent}<set-header name="{name}" '
                f'exists-action="delete" />\n'
                for name in business_header_names[:14]
            )
            result = (
                result[: core_match.start()]
                + business_snapshots
                + result[core_match.start() :]
            )
            cleanup_match = re.search(
                r'(?m)^\s*<set-header name="x-hive-organization" '
                r'exists-action="delete" />',
                result,
            )
            assert cleanup_match is not None
            result = (
                result[: cleanup_match.start()]
                + business_cleanup
                + result[cleanup_match.start() :]
            )

        missing_business_cleanup = []
        for name in business_header_names:
            count = result.count(f'name="{name}" exists-action="delete"')
            if count > 1:
                raise PolicyCompilationError(
                    f"The live parent policy has an invalid {name} provider cleanup"
                )
            if count == 0:
                missing_business_cleanup.append(name)
        if missing_business_cleanup:
            inbound_end = result.find("</inbound>")
            if inbound_end < 0:
                raise PolicyCompilationError("The live parent policy has no inbound boundary")
            additions = "".join(
                f'    <set-header name="{name}" exists-action="delete" />\n'
                for name in missing_business_cleanup
            )
            result = result[:inbound_end] + additions + result[inbound_end:]

        missing_codex_cleanup = []
        for name in codex_header_names:
            count = result.count(f'name="{name}" exists-action="delete"')
            if count > 1:
                raise PolicyCompilationError(
                    f"The live parent policy has an invalid {name} provider cleanup"
                )
            if count == 0:
                missing_codex_cleanup.append(name)
        if missing_codex_cleanup:
            inbound_end = result.find("</inbound>")
            if inbound_end < 0:
                raise PolicyCompilationError("The live parent policy has no inbound boundary")
            additions = (
                '    <choose>\n'
                '      <when condition="@((bool)context.Variables['
                '&quot;isResponsesOperation&quot;])">\n'
                + "".join(
                    f'        <set-header name="{name}" exists-action="delete" />\n'
                    for name in missing_codex_cleanup
                )
                + "      </when>\n"
                "    </choose>\n"
            )
            result = result[:inbound_end] + additions + result[inbound_end:]

        for name in snapshots:
            if result.count(f'name="{name}"') != 1:
                raise PolicyCompilationError(
                    f"The live parent policy has an invalid {name} snapshot"
                )
        for name in business_header_names + codex_header_names:
            if result.count(f'name="{name}" exists-action="delete"') != 1:
                raise PolicyCompilationError(
                    f"The live parent policy has an invalid {name} provider cleanup"
                )

        sections = {
            "outbound": ("<outbound>", "</outbound>"),
            "on-error": ("<on-error>", "</on-error>"),
        }
        replacements = {
            "outbound": {
                'context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryDepartment&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-org-name&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryOrganization&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-org-id&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryOrganizationId&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-department-id&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryDepartmentId&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-project-name&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryProject&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-project-id&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryProjectId&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-agent-name&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryAgent&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-agent-id&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryAgentId&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-user-name&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryUser&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-user-id&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryUserId&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-model-id&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryModelId&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryRequestSource&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-workflow&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryWorkflow&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-run-id&quot;, (string)context.Variables[&quot;callerRequestId&quot;])': '(string)context.Variables[&quot;telemetryRunId&quot;]',
                'int.Parse(context.Request.Headers.GetValueOrDefault(&quot;x-hive-turn-index&quot;, &quot;1&quot;))': 'int.Parse((string)context.Variables[&quot;telemetryTurnIndex&quot;])',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-model&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-model-id&quot;, &quot;unattributed&quot;))': '(string)context.Variables[&quot;telemetryModel&quot;]',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-runtime&quot;, &quot;unattributed&quot;)': '(string)context.Variables[&quot;telemetryRuntime&quot;]',
            },
            "on-error": {
                'context.Request.Headers.GetValueOrDefault(&quot;x-request-id&quot;, context.RequestId.ToString())': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;callerRequestId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-request-id&quot;, context.RequestId.ToString()))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryDepartment&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-name&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-org-name&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryOrganization&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-org-name&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-org-id&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryOrganizationId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-org-id&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-department-id&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryDepartmentId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-department-id&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-project-name&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryProject&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-project-name&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-project-id&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryProjectId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-project-id&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-agent-name&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryAgent&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-agent-name&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-agent-id&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryAgentId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-agent-id&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-user-name&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryUser&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-user-name&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-user-id&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryUserId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-user-id&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-model-id&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryModelId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-model-id&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryRequestSource&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-request-source&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-workflow&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryWorkflow&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-hive-workflow&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-run-id&quot;, context.RequestId.ToString())': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryRunId&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-hive-run-id&quot;, context.RequestId.ToString()))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-model&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryModel&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-hive-model&quot;, &quot;unattributed&quot;))',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-runtime&quot;, &quot;unattributed&quot;)': 'context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryRuntime&quot;, context.Request.Headers.GetValueOrDefault(&quot;x-hive-runtime&quot;, &quot;unattributed&quot;))',
                'new JProperty(&quot;turn_index&quot;, 1)': 'new JProperty(&quot;turn_index&quot;, int.Parse(context.Variables.GetValueOrDefault&lt;string&gt;(&quot;telemetryTurnIndex&quot;, &quot;1&quot;)))',
            },
        }
        for section_name, (start_anchor, end_anchor) in sections.items():
            start = result.find(start_anchor)
            end = result.find(end_anchor, start)
            if start < 0 or end < 0:
                raise PolicyCompilationError(
                    f"The live parent policy has no {section_name} boundary"
                )
            section = result[start:end]
            section = section.replace(
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-runtime&quot;, '
                '&quot;Microsoft Foundry via APIM&quot;)',
                'context.Request.Headers.GetValueOrDefault(&quot;x-hive-runtime&quot;, '
                '&quot;unattributed&quot;)',
            )
            for old, new in replacements[section_name].items():
                if new in section:
                    continue
                if old not in section:
                    raise PolicyCompilationError(
                        f"The live parent policy no longer matches the {section_name} telemetry anchors"
                    )
                section = section.replace(old, new)
            result = result[:start] + section + result[end:]
        return result
