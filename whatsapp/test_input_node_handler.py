"""
Unit Tests for Input Node Handler
==================================

Tests validation, extraction, correction detection, and variable substitution.
"""

import pytest
from whatsapp.input_node_handler import (
    validate_input,
    extract_field_value,
    is_correction_message,
    extract_correction_value,
    substitute_variables,
    build_question_message,
    process_input_response,
)


# ── Validation: Text ──────────────────────────────────────────────

class TestValidateText:
    def test_valid_name(self):
        ok, val, err = validate_input("Prabhu", {"type": "text", "min_length": 2})
        assert ok is True
        assert val == "Prabhu"
        assert err is None

    def test_too_short(self):
        ok, val, err = validate_input("A", {"type": "text", "min_length": 2})
        assert ok is False
        assert "2 characters" in err

    def test_too_long(self):
        ok, val, err = validate_input("x" * 600, {"type": "text", "max_length": 500})
        assert ok is False
        assert "500" in err

    def test_empty(self):
        ok, val, err = validate_input("", {"type": "text"})
        assert ok is False

    def test_whitespace_only(self):
        ok, val, err = validate_input("   ", {"type": "text"})
        assert ok is False

    def test_custom_error_message(self):
        ok, val, err = validate_input("A", {"type": "text", "min_length": 2, "error_message": "Custom error"})
        assert ok is False
        assert err == "Custom error"


# ── Validation: Number ────────────────────────────────────────────

class TestValidateNumber:
    def test_valid_integer(self):
        ok, val, err = validate_input("21", {"type": "number"})
        assert ok is True
        assert val == 21

    def test_valid_float(self):
        ok, val, err = validate_input("21.5", {"type": "number"})
        assert ok is True
        assert val == 21.5

    def test_with_text_noise(self):
        ok, val, err = validate_input("age: 21", {"type": "number"})
        assert ok is True
        assert val == 21

    def test_invalid_text(self):
        ok, val, err = validate_input("twenty one", {"type": "number"})
        assert ok is False

    def test_min_max(self):
        ok, val, err = validate_input("150", {"type": "number", "min_value": 1, "max_value": 120})
        assert ok is False
        assert "120" in err

    def test_negative_below_min(self):
        ok, val, err = validate_input("0", {"type": "number", "min_value": 1})
        assert ok is False


# ── Validation: Email ─────────────────────────────────────────────

class TestValidateEmail:
    def test_valid(self):
        ok, val, err = validate_input("test@example.com", {"type": "email"})
        assert ok is True
        assert val == "test@example.com"

    def test_invalid(self):
        ok, val, err = validate_input("not-an-email", {"type": "email"})
        assert ok is False

    def test_uppercase_normalized(self):
        ok, val, err = validate_input("Test@Example.COM", {"type": "email"})
        assert ok is True
        assert val == "test@example.com"


# ── Validation: Phone ────────────────────────────────────────────

class TestValidatePhone:
    def test_valid_10_digits(self):
        ok, val, err = validate_input("9876543210", {"type": "phone"})
        assert ok is True
        assert val == "9876543210"

    def test_with_formatting(self):
        ok, val, err = validate_input("+91 98765 43210", {"type": "phone"})
        assert ok is True
        assert val == "919876543210"

    def test_too_short(self):
        ok, val, err = validate_input("12345", {"type": "phone"})
        assert ok is False


# ── Validation: Enum ──────────────────────────────────────────────

class TestValidateEnum:
    def test_exact_match(self):
        ok, val, err = validate_input("male", {"type": "enum", "options": ["Male", "Female", "Other"]})
        assert ok is True
        assert val == "Male"

    def test_numeric_selection(self):
        ok, val, err = validate_input("2", {"type": "enum", "options": ["Male", "Female", "Other"]})
        assert ok is True
        assert val == "Female"

    def test_fuzzy_match(self):
        ok, val, err = validate_input("femal", {"type": "enum", "options": ["Male", "Female", "Other"]})
        assert ok is True
        assert val == "Female"

    def test_no_match(self):
        ok, val, err = validate_input("xyz", {"type": "enum", "options": ["Male", "Female", "Other"]})
        assert ok is False
        assert "Male" in err


# ── Validation: Pincode ───────────────────────────────────────────

class TestValidatePincode:
    def test_valid(self):
        ok, val, err = validate_input("500001", {"type": "pincode"})
        assert ok is True
        assert val == "500001"

    def test_invalid_length(self):
        ok, val, err = validate_input("12345", {"type": "pincode"})
        assert ok is False

    def test_with_spaces(self):
        ok, val, err = validate_input("500 001", {"type": "pincode"})
        assert ok is True
        assert val == "500001"


# ── Validation: Regex ─────────────────────────────────────────────

class TestValidateRegex:
    def test_valid_pattern(self):
        ok, val, err = validate_input("ABC123", {"type": "regex", "pattern": r"^[A-Z]{3}\d{3}$"})
        assert ok is True

    def test_invalid_pattern(self):
        ok, val, err = validate_input("abc", {"type": "regex", "pattern": r"^[A-Z]{3}\d{3}$"})
        assert ok is False


# ── Smart Extraction ──────────────────────────────────────────────

class TestExtraction:
    def test_name_with_prefix_my_name_is(self):
        val = extract_field_value("My name is Prabhu", "name")
        assert val == "Prabhu"

    def test_name_with_prefix_i_am(self):
        val = extract_field_value("I am Prabhu", "name")
        assert val == "Prabhu"

    def test_name_plain(self):
        val = extract_field_value("Prabhu", "name")
        assert val == "Prabhu"

    def test_name_with_prefix_call_me(self):
        val = extract_field_value("Call me Prabhu", "name")
        assert val == "Prabhu"

    def test_age_with_prefix(self):
        val = extract_field_value("I am 21 years old", "number")
        assert val == "21"

    def test_age_plain(self):
        val = extract_field_value("21", "number")
        assert val == "21"

    def test_age_with_text(self):
        val = extract_field_value("My age is 21", "number")
        assert val == "21"


# ── Correction Detection ─────────────────────────────────────────

class TestCorrection:
    def test_sorry_prefix(self):
        assert is_correction_message("sorry 22") is True

    def test_actually_prefix(self):
        assert is_correction_message("actually it's 22") is True

    def test_not_correction(self):
        assert is_correction_message("22") is False

    def test_extract_sorry(self):
        val = extract_correction_value("sorry 22")
        assert val == "22"

    def test_extract_actually_its(self):
        val = extract_correction_value("actually it's Prabhu")
        assert val == "Prabhu"

    def test_extract_i_meant(self):
        val = extract_correction_value("i meant 23")
        assert val == "23"

    def test_extract_no_comma(self):
        val = extract_correction_value("no, 25")
        assert val == "25"


# ── Variable Substitution ────────────────────────────────────────

class TestSubstitution:
    def test_single_variable(self):
        result = substitute_variables("Hello {{name}}!", {"name": "Prabhu"})
        assert result == "Hello Prabhu!"

    def test_multiple_variables(self):
        result = substitute_variables(
            "{{name}} is {{age}} years old",
            {"name": "Prabhu", "age": 21},
        )
        assert result == "Prabhu is 21 years old"

    def test_missing_variable(self):
        result = substitute_variables("Hello {{name}}, your city is {{city}}", {"name": "Prabhu"})
        assert result == "Hello Prabhu, your city is {{city}}"

    def test_no_variables(self):
        result = substitute_variables("No variables here", {"name": "Prabhu"})
        assert result == "No variables here"

    def test_empty_collected(self):
        result = substitute_variables("Hello {{name}}!", {})
        assert result == "Hello {{name}}!"


# ── Question Builder ─────────────────────────────────────────────

class TestQuestionBuilder:
    def test_basic_question(self):
        result = build_question_message(
            {"question": "What is your name?"},
            {},
        )
        assert "What is your name?" in result

    def test_with_variable_substitution(self):
        result = build_question_message(
            {"question": "Thanks {{name}}, what is your age?"},
            {"name": "Prabhu"},
        )
        assert "Thanks Prabhu" in result

    def test_with_error_message(self):
        result = build_question_message(
            {"body": "What is your age?"},
            {},
            error_message="Please enter a valid number.",
        )
        assert "valid number" in result
        assert "What is your age?" in result
        assert result.startswith("Please enter a valid number.")

    def test_enum_options(self):
        result = build_question_message(
            {
                "question": "What is your gender?",
                "validation": {"type": "enum", "options": ["Male", "Female", "Other"]},
            },
            {},
        )
        assert "1. Male" in result
        assert "2. Female" in result
        assert "3. Other" in result

    def test_skip_hint(self):
        result = build_question_message(
            {
                "question": "What is your email?",
                "skip_keyword": "skip",
                "required": False,
            },
            {},
        )
        assert "skip" in result


# ── Edge Cases ────────────────────────────────────────────────────

class TestEdgeCases:
    def test_split_message_my_name_is(self):
        """Split message: 'My name is' should fail validation with min_length=2."""
        extracted = extract_field_value("My name is", "name")
        ok, val, err = validate_input(extracted, {"type": "text", "min_length": 2})
        # "My name is" with name prefix stripped → empty → fails
        assert ok is False

    def test_multi_field_single_message(self):
        """Multi-field in one message: accept only expected field."""
        # When asking for name, only extract name
        extracted = extract_field_value("My name is Prabhu, age is 21", "name")
        ok, val, err = validate_input(extracted, {"type": "text", "min_length": 2})
        assert ok is True
        # It will accept the full text after prefix strip
        assert "Prabhu" in val

    def test_wrong_type_for_age(self):
        """User sends city name when age is asked."""
        ok, val, err = validate_input("Hyderabad", {"type": "number", "min_value": 1, "max_value": 120})
        assert ok is False

    def test_unicode_name(self):
        ok, val, err = validate_input("प्रभु", {"type": "text", "min_length": 2})
        assert ok is True
        assert val == "प्रभु"
