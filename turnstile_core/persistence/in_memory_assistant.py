from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from .repository_support import attach_charts, report_for_viewer


class InMemoryAssistantRepositoryMixin:
    pinned_reports: list[dict[str, Any]]
    pinned_charts: list[dict[str, Any]]
    conversations: list[dict[str, Any]]
    assistant_setting: dict[str, Any]

    def list_pinned_reports(self, owner_id: str) -> Sequence[dict[str, Any]]:
        reports = sorted(
            (
                report_for_viewer(item, owner_id)
                for item in self.pinned_reports
                if item["created_by"] == owner_id or item["visibility"] == "public"
            ),
            key=lambda item: (
                item["owner_id"] != owner_id,
                item["position"],
                item["created_at"],
            ),
        )
        return attach_charts(reports, self._sorted_charts())

    def get_pinned_report(self, report_id: UUID, owner_id: str) -> dict[str, Any] | None:
        report = next(
            (
                item
                for item in self.pinned_reports
                if item["id"] == report_id
                and (item["created_by"] == owner_id or item["visibility"] == "public")
            ),
            None,
        )
        if report is None:
            return None
        return attach_charts([report_for_viewer(report, owner_id)], self._sorted_charts())[0]

    def _sorted_charts(self) -> list[dict[str, Any]]:
        return sorted(
            self.pinned_charts, key=lambda item: (item["position"], item["created_at"])
        )

    def _owns(self, report_id: UUID, owner_id: str) -> bool:
        return any(
            item["id"] == report_id and item["created_by"] == owner_id
            for item in self.pinned_reports
        )

    def create_pinned_report(
        self,
        *,
        owner_id: str,
        title: str,
        description: str,
        original_question: str,
        chart: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        owned = [item for item in self.pinned_reports if item["created_by"] == owner_id]
        report = {
            "id": uuid4(),
            "owner_id": owner_id,
            "created_by": owner_id,
            "visibility": "private",
            "title": title,
            "description": description,
            "position": max((item["position"] for item in owned), default=-1) + 1,
            "layout": {"version": 1, "spans": {}, "row_heights": {}},
            "created_at": now,
            "updated_at": now,
        }
        self.pinned_reports.append(report)
        self.pinned_charts.append(
            {
                "id": uuid4(),
                "report_id": report["id"],
                "original_question": original_question,
                "chart": dict(chart),
                "position": 0,
                "created_at": now,
                "updated_at": now,
            }
        )
        return attach_charts([report_for_viewer(report, owner_id)], self._sorted_charts())[0]

    def add_chart_to_pinned_report(
        self,
        *,
        report_id: UUID,
        owner_id: str,
        original_question: str,
        chart: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if not self._owns(report_id, owner_id):
            return None
        now = datetime.now(UTC)
        query = chart.get("query")
        siblings = [item for item in self.pinned_charts if item["report_id"] == report_id]
        # Mirrors the unique index: the same query pinned twice into one report is the
        # same card, so the second pin refreshes it rather than duplicating it.
        existing = next(
            (item for item in siblings if item["chart"].get("query") == query), None
        )
        if existing is not None:
            existing.update(
                original_question=original_question, chart=dict(chart), updated_at=now
            )
            return dict(existing)
        row = {
            "id": uuid4(),
            "report_id": report_id,
            "original_question": original_question,
            "chart": dict(chart),
            "position": max((item["position"] for item in siblings), default=-1) + 1,
            "created_at": now,
            "updated_at": now,
        }
        self.pinned_charts.append(row)
        for report in self.pinned_reports:
            if report["id"] == report_id:
                report["updated_at"] = now
        return dict(row)

    def update_pinned_report(
        self, report_id: UUID, owner_id: str, title: str, description: str
    ) -> dict[str, Any] | None:
        for item in self.pinned_reports:
            if item["id"] == report_id and item["created_by"] == owner_id:
                item.update(title=title, description=description, updated_at=datetime.now(UTC))
                return attach_charts(
                    [report_for_viewer(item, owner_id)], self._sorted_charts()
                )[0]
        return None

    def update_pinned_report_visibility(
        self, report_id: UUID, owner_id: str, visibility: str
    ) -> dict[str, Any] | None:
        for item in self.pinned_reports:
            if item["id"] == report_id and item["created_by"] == owner_id:
                item.update(visibility=visibility, updated_at=datetime.now(UTC))
                return attach_charts(
                    [report_for_viewer(item, owner_id)], self._sorted_charts()
                )[0]
        return None

    def update_pinned_report_layout(
        self, report_id: UUID, owner_id: str, layout: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        for item in self.pinned_reports:
            if item["id"] == report_id and item["created_by"] == owner_id:
                item.update(layout=dict(layout), updated_at=datetime.now(UTC))
                return attach_charts(
                    [report_for_viewer(item, owner_id)], self._sorted_charts()
                )[0]
        return None

    def replace_pinned_chart_data(
        self, chart_id: UUID, owner_id: str, chart: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        for item in self.pinned_charts:
            if item["id"] == chart_id and self._owns(item["report_id"], owner_id):
                item.update(chart=dict(chart), updated_at=datetime.now(UTC))
                return dict(item)
        return None

    def delete_pinned_report(self, report_id: UUID, owner_id: str) -> bool:
        if not self._owns(report_id, owner_id):
            return False
        self.pinned_reports = [item for item in self.pinned_reports if item["id"] != report_id]
        # Mirrors ON DELETE CASCADE.
        self.pinned_charts = [
            item for item in self.pinned_charts if item["report_id"] != report_id
        ]
        return True

    def delete_pinned_chart(self, chart_id: UUID, owner_id: str) -> bool:
        target = next((item for item in self.pinned_charts if item["id"] == chart_id), None)
        if target is None or not self._owns(target["report_id"], owner_id):
            return False
        self.pinned_charts = [
            item for item in self.pinned_charts if item["id"] != chart_id
        ]
        for report in self.pinned_reports:
            if report["id"] != target["report_id"]:
                continue
            layout = dict(report.get("layout", {}))
            spans = dict(layout.get("spans", {}))
            spans.pop(str(chart_id), None)
            spans.pop(chart_id, None)
            report.update(
                layout={**layout, "spans": spans},
                updated_at=datetime.now(UTC),
            )
        return True

    def reorder_pinned_reports(self, owner_id: str, ordered_ids: Sequence[UUID]) -> None:
        order = {report_id: index for index, report_id in enumerate(ordered_ids)}
        for item in self.pinned_reports:
            if item["created_by"] == owner_id and item["id"] in order:
                item["position"] = order[item["id"]]

    def reorder_pinned_charts(
        self, report_id: UUID, owner_id: str, ordered_ids: Sequence[UUID]
    ) -> None:
        order = {chart_id: index for index, chart_id in enumerate(ordered_ids)}
        for item in self.pinned_charts:
            if (
                item["report_id"] == report_id
                and item["id"] in order
                and self._owns(report_id, owner_id)
            ):
                item["position"] = order[item["id"]]

    def list_conversations(self, owner_id: str, limit: int) -> Sequence[dict[str, Any]]:
        owned = [item for item in self.conversations if item["created_by"] == owner_id]
        owned.sort(key=lambda item: item["updated_at"], reverse=True)
        return [self._conversation_summary(item) for item in owned[:limit]]

    def get_conversation(
        self, conversation_id: UUID, owner_id: str
    ) -> dict[str, Any] | None:
        for item in self.conversations:
            if item["id"] == conversation_id and item["created_by"] == owner_id:
                conversation = self._conversation_summary(item)
                conversation["turns"] = [dict(turn) for turn in item["turns"]]
                return conversation
        return None

    def append_conversation_turn(
        self,
        *,
        conversation_id: UUID,
        owner_id: str,
        title: str,
        question: str,
        reply: Mapping[str, Any],
    ) -> None:
        now = datetime.now(UTC)
        existing = next(
            (
                item
                for item in self.conversations
                if item["id"] == conversation_id and item["created_by"] == owner_id
            ),
            None,
        )
        if existing is None:
            # Matches the SQL upsert's ownership guard: an id that belongs to somebody
            # else must not be adopted, and must not raise either.
            if any(item["id"] == conversation_id for item in self.conversations):
                return
            existing = {
                "id": conversation_id,
                "owner_id": owner_id,
                "created_by": owner_id,
                "title": title,
                "title_source": "question",
                "created_at": now,
                "updated_at": now,
                "turns": [],
            }
            self.conversations.append(existing)
        existing["updated_at"] = now
        existing["turns"].append(
            {
                "position": len(existing["turns"]),
                "question": question,
                "reply": dict(reply),
                "created_at": now,
            }
        )

    def rename_conversation(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> dict[str, Any] | None:
        for item in self.conversations:
            if item["id"] == conversation_id and item["created_by"] == owner_id:
                item.update(
                    title=title, title_source="user", updated_at=datetime.now(UTC)
                )
                return self._conversation_summary(item)
        return None

    def summarise_conversation_title(
        self, conversation_id: UUID, owner_id: str, title: str
    ) -> dict[str, Any] | None:
        # Same guard as the SQL predicate: only a placeholder is replaceable, and
        # `updated_at` is left alone so naming a thread does not reorder the list.
        for item in self.conversations:
            if (
                item["id"] == conversation_id
                and item["created_by"] == owner_id
                and item.get("title_source", "question") == "question"
            ):
                item.update(title=title, title_source="model")
                return self._conversation_summary(item)
        return None

    def delete_conversation(self, conversation_id: UUID, owner_id: str) -> bool:
        before = len(self.conversations)
        self.conversations = [
            item
            for item in self.conversations
            if not (item["id"] == conversation_id and item["created_by"] == owner_id)
        ]
        return len(self.conversations) < before

    def assistant_settings(self) -> dict[str, Any]:
        return dict(self.assistant_setting)

    def save_assistant_settings(
        self, *, model_id: UUID | None, auto_title: bool, updated_by: str,
        api_format: str | None = None,
    ) -> dict[str, Any]:
        self.assistant_setting = {
            "model_id": model_id,
            "auto_title": auto_title,
            "api_format": api_format,
            "updated_at": datetime.now(UTC),
            "updated_by": updated_by,
        }
        return dict(self.assistant_setting)

    @staticmethod
    def _conversation_summary(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": item["id"],
            "owner_id": item["created_by"],
            "title": item["title"],
            "title_source": item.get("title_source", "question"),
            "turn_count": len(item["turns"]),
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
        }
