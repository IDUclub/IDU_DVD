"""Unit tests for src/dvd_service/modules/identity — key derivation helpers.

Focused on ``extract_version_from_name`` (the trailing-year version heuristic used by the
upload/update endpoints); the other helpers are exercised through the service tests.
"""

from __future__ import annotations

import pytest

from src.dvd_service.modules.identity import extract_version_from_name


class TestExtractVersionFromName:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("СП 2.13130.2020", "2020"),  # 5-digit run "13130" is skipped
            ("ГОСТ 12.1.004-91", None),  # no standalone 4-digit group
            ("СП 42.13330.2016 (СНиП 2.07.01-89*)", "2016"),
            ("Приказ 123 от 2021 года, ред. 2023", "2023"),  # the LAST group wins
            ("Документ без цифр", None),
            ("", None),
            ("12345", None),  # longer digit runs never match partially
            ("1999", "1999"),
            # a document number is not an edition year
            ("СП 2.4.3648-20", None),
            ("СП 2.4.2.4283-26", None),
            ("СанПиН 1.2.3685-21", None),
            ("СП 2.4.3648-20, ред. 2022", "2022"),
            ("Постановление от 4 декабря 2017 г. N 525", "2017"),
        ],
    )
    def test_extracts_last_standalone_year(self, name, expected):
        assert extract_version_from_name(name) == expected

    def test_none_like_input(self):
        assert extract_version_from_name(None) is None
