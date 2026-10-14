"""Edit request models (plan 14.4). Strict: unknown fields are errors, never ignored.

There is deliberately NO ``approved`` / ``approval`` field anywhere: approval is a property of the
trusted local store (``editing.approval``), never of a request.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from svweave.domain.ids import canonical_hash

MAX_EDITS = 50
MAX_TEXT_BYTES = 60000  # the gateway caps a whole request at 64 KiB
KEY_PATTERN = r"^[A-Za-z0-9._:-]{8,128}$"
HEX64 = r"^[0-9a-f]{64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SymbolReplacement(_Strict):
    """Replace the whole text of ONE declaration (identified by its declaration id in the base
    build) with ``new_body``. A bare name is never accepted (EDT-03)."""

    kind: Literal["symbol_replacement"]
    declaration_id: str = Field(min_length=1, max_length=256)
    new_body: str = Field(max_length=MAX_TEXT_BYTES)


class ExactRange(_Strict):
    """Replace ``[start_byte, end_byte)`` of a snapshot file, but only if those bytes hash to
    ``expected_sha256`` (compare-and-set on the original slice)."""

    kind: Literal["exact_range"]
    logical_path: str = Field(min_length=1, max_length=1024)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    expected_sha256: str = Field(pattern=HEX64)
    replacement: str = Field(max_length=MAX_TEXT_BYTES)


class SlangSymbolEdit(_Strict):
    """Replace the whole declaration of ONE task / function / constructor, located BY NAME with
    slang's semantic database (ADR-013). Give exactly one of ``qualified_name`` (for example
    ``pkg::cls::run_phase``; a suffix such as ``cls::run_phase`` is accepted but a bare name that
    matches several declarations is ``AMBIGUOUS_SYMBOL``) or ``symbol_id``. ``replacement`` is the
    full declaration text, from the ``task`` / ``function`` keyword to ``endtask`` /
    ``endfunction``. ``expected_sha256`` (optional) must match the current declaration bytes."""

    kind: Literal["slang_symbol"]
    qualified_name: str | None = Field(default=None, min_length=1, max_length=512)
    symbol_id: str | None = Field(default=None, pattern=HEX64)
    replacement: str = Field(max_length=MAX_TEXT_BYTES)
    expected_sha256: str | None = Field(default=None, pattern=HEX64)

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> SlangSymbolEdit:
        if (self.qualified_name is None) == (self.symbol_id is None):
            raise ValueError("give exactly one of qualified_name, symbol_id")
        return self


EditSpec = Annotated[
    SymbolReplacement | ExactRange | SlangSymbolEdit, Field(discriminator="kind")
]


class PrepareRequest(_Strict):
    base_build_id: str = Field(pattern=HEX64)
    snapshot_id: str = Field(pattern=HEX64)
    edits: list[EditSpec] = Field(min_length=1, max_length=MAX_EDITS)
    validate_profiles: list[str] = Field(default_factory=list, max_length=20)
    request_key: str = Field(pattern=KEY_PATTERN)

    def payload_hash(self) -> str:
        """Identity of the request WITHOUT its idempotency key (same key + other payload =
        IDEMPOTENCY_CONFLICT)."""
        d = self.model_dump(mode="json")
        d.pop("request_key")
        return canonical_hash({"kind": "edit-prepare", "payload": d})
