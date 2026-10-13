"""Synthetic starter project for AIK-083. Incorrect on purpose; no answer is bundled."""
import csv
import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    policy = json.loads((root / "policy.json").read_text(encoding="utf-8"))
    with (root / "orders.csv").open(newline="", encoding="utf-8") as stream:
        orders = list(csv.DictReader(stream))
    net = sum(int(row["subtotal_krw"]) - int(row["discount_krw"]) for row in orders)
    # KNOWN BUG: rounds total VAT rather than each line, and omits the item breakdown.
    vat = round(net * policy["vat_rate_bps"] / 10000)
    (root / policy["output_file"]).write_text(
        json.dumps({"net_krw": net, "vat_krw": vat, "gross_krw": net + vat}), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
