from sg_mcp_server._engine._errors import (
    EngineError,
    QueryError,
    SchemaError,
    ValidationError,
    _capped,
)

_EXPECTED_CAPPED_VALUES = [
    'value_0',
    'value_1',
    'value_2',
    'value_3',
    'value_4',
    'value_5',
    'value_6',
    'value_7',
    'value_8',
    'value_9',
    'value_10',
    'value_11',
    'value_12',
    'value_13',
    'value_14',
    'value_15',
    'value_16',
    'value_17',
    'value_18',
    'value_19',
    'value_20',
    'value_21',
    'value_22',
    'value_23',
    'value_24',
    'value_25',
    'value_26',
    'value_27',
    'value_28',
    'value_29',
    'value_30',
    'value_31',
    'value_32',
    'value_33',
    'value_34',
    'value_35',
    'value_36',
    'value_37',
    'value_38',
    'value_39',
]


class TestCapped:
    def test_keeps_short_alternatives(self) -> None:
        values = ['one', 'two']
        assert _capped(values) == values

    def test_never_fabricates_entries(self) -> None:
        values = [f'value_{index}' for index in range(45)]

        assert _capped(values) == _EXPECTED_CAPPED_VALUES
        assert all(value in values for value in _capped(values))


class TestEngineError:
    def test_subclasses_share_one_catch_boundary(self) -> None:
        errors = [QueryError(), SchemaError(), ValidationError('invalid')]
        assert all(isinstance(error, EngineError) for error in errors)


class TestValidationError:
    def test_carries_message_alternatives_and_unknown_name(self) -> None:
        error = ValidationError(
            'invalid field',
            unknown_name=True,
        )

        assert str(error) == 'invalid field'
        assert error.message == 'invalid field'
        assert error.alternatives == {}
        assert error.alternatives_omitted == {}
        assert error.unknown_name is True

    def test_truncated_alternatives_move_guidance_to_the_message(self) -> None:
        error = ValidationError(
            'invalid field',
            alternatives={'fields': [f'value_{index}' for index in range(45)]},
        )

        assert error.alternatives == {'fields': _EXPECTED_CAPPED_VALUES}
        assert error.alternatives_omitted == {'fields': 5}
        assert error.message.endswith('only the first 40 are included')
        assert 'enumerate' not in error.message
