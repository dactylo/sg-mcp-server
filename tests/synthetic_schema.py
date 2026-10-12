import pickle
from pathlib import Path

_FIELD_TOTALS = {'Shot': 81, 'Task': 59}
_STATUSES: dict[str, dict[str, str]] = {
    'active': {'act': 'Active', 'dis': 'Disabled'},
    'attachment': {'na': 'N/A', 'fin': 'Final'},
    'note': {'opn': 'Open', 'ip': 'In Progress', 'clsd': 'Closed'},
    'production': {'wtg': 'Waiting to Start', 'ip': 'In Progress', 'fin': 'Final'},
    'review': {'na': 'N/A', 'rev': 'Pending Review', 'apr': 'Approved'},
}
# Field name -> (data type, label, detail). Detail identifies linked entity types or statuses.
_ENTITIES: dict[str, dict[str, tuple[str, str, tuple[str, ...] | str | None]]] = {
    'Asset': {
        'assets': ('multi_entity', 'Sub Assets', ('Asset',)),
        'code': ('text', 'Asset Name', None),
        'description': ('text', 'Description', None),
        'image': ('image', 'Thumbnail', None),
        'project': ('entity', 'Project', ('Project',)),
        'sg_status_list': ('status_list', 'Status', 'production'),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'Attachment': {
        'description': ('text', 'Description', None),
        'display_name': ('text', 'Display Name', None),
        'image': ('image', 'Thumbnail', None),
        'metadata': ('text', 'Metadata', None),
        'project': ('entity', 'Project', ('Project',)),
        'sg_status_list': ('status_list', 'Status', 'attachment'),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'CutItem': {
        'code': ('text', 'Cut Item Name', None),
        'description': ('text', 'Description', None),
        'edit_in': ('number', 'Edit In', None),
        'image': ('image', 'Thumbnail', None),
        'project': ('entity', 'Project', ('Project',)),
        'updated_at': ('date_time', 'Date Updated', None),
        'version': ('entity', 'Version', ('Version',)),
    },
    'Department': {
        'code': ('text', 'Code', None),
        'color': ('color', 'Color', None),
        'image': ('image', 'Thumbnail', None),
        'name': ('text', 'Department Name', None),
        'sg_status_list': ('status_list', 'Status', 'active'),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'EventLogEntry': {
        'description': ('text', 'Description', None),
        # A single-entity link without valid types exercises reflected type matching.
        'entity': ('entity', 'Link', ()),
        'event_type': ('text', 'Event Type', None),
        'image': ('image', 'Thumbnail', None),
        'meta': ('serializable', 'Meta', None),
        'project': ('entity', 'Project', ('Project',)),
    },
    'Note': {
        'content': ('text', 'Body', None),
        'image': ('image', 'Thumbnail', None),
        'meta': ('jsonb', 'Meta', None),
        'project': ('entity', 'Project', ('Project',)),
        'sg_status_list': ('status_list', 'Status', 'note'),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'Playlist': {
        'code': ('text', 'Playlist Name', None),
        'description': ('text', 'Description', None),
        'image': ('image', 'Thumbnail', None),
        'project': ('entity', 'Project', ('Project',)),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'Project': {
        'code': ('text', 'Code', None),
        'color': ('color', 'Color', None),
        'duration': ('number', 'Duration', None),
        'image': ('image', 'Thumbnail', None),
        'name': ('text', 'Project Name', None),
        'start_date': ('date', 'Start Date', None),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'Shot': {
        'assets': ('multi_entity', 'Assets', ('Asset',)),
        'code': ('text', 'Shot Code', None),
        'description': ('text', 'Description', None),
        'image': ('image', 'Thumbnail', None),
        'project': ('entity', 'Project', ('Project',)),
        'sg_cut_in': ('number', 'Cut In', None),
        'sg_status_list': ('status_list', 'Status', 'production'),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'Status': {
        'code': ('text', 'Status Code', None),
        'name': ('text', 'Status Name', None),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'Step': {
        'code': ('text', 'Step Name', None),
        'color': ('color', 'Color', None),
        'description': ('text', 'Description', None),
        'entity_type': ('entity_type', 'Entity Type', None),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'Task': {
        'color': ('color', 'Color', None),
        'content': ('text', 'Task Name', None),
        'duration': ('duration', 'Duration', None),
        'entity': ('entity', 'Link', ('Asset', 'Shot')),
        'image': ('image', 'Thumbnail', None),
        'project': ('entity', 'Project', ('Project',)),
        'sg_status_list': ('status_list', 'Status', 'production'),
        'start_date': ('date', 'Start Date', None),
        'updated_at': ('date_time', 'Date Updated', None),
    },
    'Version': {
        'code': ('text', 'Version Name', None),
        'description': ('text', 'Description', None),
        'entity': ('entity', 'Link', ('Asset', 'Shot')),
        'image': ('image', 'Thumbnail', None),
        'project': ('entity', 'Project', ('Project',)),
        'sg_status_list': ('status_list', 'Status', 'review'),
        'sg_uploaded_movie': ('url', 'Uploaded Movie', None),
        'updated_at': ('date_time', 'Date Updated', None),
    },
}


def _envelope(value: object, *, editable: bool = True) -> dict[str, object]:
    return {'value': value, 'editable': editable}


def _field(
    entity_type: str,
    data_type: str,
    label: str,
    detail: tuple[str, ...] | str | None,
    *,
    editable: bool = True,
) -> dict[str, object]:
    properties = {
        'default_value': _envelope(None),
        'summary_default': _envelope('none'),
    }
    if data_type in {'entity', 'multi_entity'}:
        assert isinstance(detail, tuple)
        properties['valid_types'] = _envelope(list(detail))
    elif data_type == 'status_list':
        assert isinstance(detail, str)
        statuses = _STATUSES[detail]
        properties['valid_values'] = _envelope(list(statuses))
        properties['display_values'] = _envelope(dict(statuses))
        properties['hidden_values'] = _envelope([])

    return {
        'name': _envelope(label),
        'description': _envelope(''),
        'custom_metadata': _envelope(''),
        'entity_type': _envelope(entity_type),
        'data_type': _envelope(data_type),
        'editable': _envelope(editable, editable=editable),
        'mandatory': _envelope(value=False),
        'unique': _envelope(value=False),
        'properties': properties,
        'visible': _envelope(value=True),
        'ui_value_displayable': _envelope(value=True),
    }


def build_schema() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    fields: dict[str, dict[str, object]] = {}
    entities: dict[str, dict[str, object]] = {}
    for entity_type, declared_fields in _ENTITIES.items():
        entity_fields: dict[str, object] = {
            'id': _field(entity_type, 'number', 'Id', None, editable=False),
        }
        for field_name, (data_type, label, detail) in declared_fields.items():
            entity_fields[field_name] = _field(entity_type, data_type, label, detail)
        for index in range(_FIELD_TOTALS.get(entity_type, 0) - len(entity_fields)):
            field_name = f'sg_extra_{index:03}'
            entity_fields[field_name] = _field(entity_type, 'text', f'Extra {index:03}', None)
        fields[entity_type] = entity_fields
        entities[entity_type] = {
            'name': _envelope(entity_type),
            'visible': _envelope(value=True),
        }
    return fields, entities


def write_schema_pickles(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    fields_path = directory / 'synthetic-schema-fields.pickle'
    entities_path = directory / 'synthetic-schema-entities.pickle'
    fields, entities = build_schema()
    with fields_path.open('wb') as handle:
        pickle.dump(fields, handle)
    with entities_path.open('wb') as handle:
        pickle.dump(entities, handle)
    return fields_path, entities_path
