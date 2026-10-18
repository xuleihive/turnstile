from tests.support.paths import FRONTEND_SOURCE


def test_mapping_row_uses_a_separate_field_for_each_input_or_select() -> None:
    source = (FRONTEND_SOURCE / "pages/directory-sync-panel.tsx").read_text()
    mapping = source.split("function MappingEditor", 1)[1].split("function BindingDialog", 1)[0]
    assert '<div key={index} className="directory-mapping-row">' in mapping
    assert '<Field key={index} className="directory-mapping-row">' not in mapping
    assert "<Field><FieldLabel htmlFor={`sync-group-${index}`}>" in mapping
    assert "<Field><FieldLabel>目标部门或团队</FieldLabel><DirectorySelect" in mapping
    assert "<Field><FieldLabel>成员范围</FieldLabel><DirectorySelect" in mapping
    assert mapping.count("</Field>") == 3
