"""Hardware constants for PeriPage A6 304dpi."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    """Print-head geometry for one PeriPage SKU."""

    name: str
    dpi: int
    row_width: int
    row_bytes: int
    ascii_columns: int
    printable_width_mm: float

    def __post_init__(self) -> None:
        expected = (self.row_width + 7) // 8
        if self.row_bytes != expected:
            raise ValueError(
                f"row_bytes={self.row_bytes} does not match row_width={self.row_width}"
            )


# 58 mm roll, ~48.5 mm printable. Firmware identifies as V2.11_304dpi.
A6_304 = Model(
    name="PeriPage A6 304dpi",
    dpi=304,
    row_width=576,
    row_bytes=72,
    ascii_columns=48,
    printable_width_mm=48.5,
)
