# Verified MCX energy monetary units

Verified on 27 October 2026. The paper engine expands `lots * lot_size` first.
The extra `price_multiplier` is therefore **1** when the broker lot already
contains the physical contract quantity below. Applying the broker quantity
multiplier again would square exposure.

| MCX symbol | Physical quantity in one lot | Price quotation |
|---|---:|---|
| CRUDEOIL | 100 barrels | INR per barrel |
| CRUDEOILM | 10 barrels | INR per barrel |
| NATURALGAS | 1,250 MMBtu | INR per MMBtu |
| NATGASMINI | 250 MMBtu | INR per MMBtu |

5paisa explicitly defines its native `Multiplier` as a **total quantity**
conversion, rather than a second monetary factor. The current public master
contains matching `LotSize` and `Multiplier` for all four products (CE, PE and
XX futures). We require both native fields to match the specification quantity.
The original positive native multiplier is retained as
`broker_quantity_multiplier`, separately from the effective price multiplier.

Kotak requires the same exact product and lot quantity, explicitly matching
`pPriceUnits` and `pTrdUnits`, compatible delivery units where supplied, and
unity `dPriceQuatation` plus any supplied numerator/denominator factors.
A native `lMultiplier=-1` is not used to guess a conversion. Missing or
mismatched evidence leaves the contract visible but ineligible for paper fills.

Other MCX commodities remain unverified unless a checked normalized
`price_multiplier` is explicitly provided by the operator. Existing cached
broker-master rows without conversion provenance keep their identity and
visibility, but their old monetary factor is disabled until refreshed.
Persisted trade legs and history are never rewritten by catalog loading.

## Primary references

- [5paisa scrip master field definitions](https://xstream.5paisa.com/dev-docs/docFundamentals/scrip-master): `LotSize`, rupee `TickSize`, and quantity `Multiplier`.
- [MCX Crude Oil products and current specifications](https://www.mcxindia.com/products/energy/crude-oil).
- [MCX Natural Gas products and current specifications](https://www.mcxindia.com/products/energy/natural-gas).
- [Crude Oil futures, January 2026 onwards](https://www.mcxindia.com/docs/default-source/products/contract-specification/crude-oil/crude-oil-january-2026-contract-onwards267be8c1-650a-4baa-aabd-ffcc9364c100.pdf).
- [Crude Oil Mini futures, January 2026 onwards](https://www.mcxindia.com/docs/default-source/products/contract-specification/crude-oil-mini/crude-oil-mini-january-2026-contract-onwardsdbdf4c09-a735-4396-a2f5-091dad12f14f.pdf).
- [Natural Gas futures, January 2026 onwards](https://www.mcxindia.com/docs/default-source/products/contract-specification/natural-gas/natural-gas-january-2026-contract-onwards2ff2141b-f6e4-4b6a-9f0e-59f260cecf9b.pdf).
- [Natural Gas Mini futures, January 2026 onwards](https://www.mcxindia.com/docs/default-source/products/contract-specification/natural-gas-mini-option/natural-gas-mini-january-2026-contract-onwards.pdf).
- [Crude Oil options, March 2026 onwards](https://www.mcxindia.com/docs/default-source/products/contract-specification/crude-oil-options/crudeoil-%28100-barrels%29-options-march-2026-contract-onwards4f4aed50-0f8d-46a4-8f1b-efba7c9b9479.pdf).
- [Crude Oil Mini options, March 2026 onwards](https://www.mcxindia.com/docs/default-source/products/contract-specification/crudeoil-mini-options/crude-oil-mini-%2810-barrels%29-options-march-2026-contract-onwards.pdf).
- [Natural Gas options, March 2026 onwards](https://www.mcxindia.com/docs/default-source/products/contract-specification/natural-gas-option/natural-gas-%281250-mmbtu%29-options-march-2026-contract-onwards.pdf).
- [Natural Gas Mini options, March 2026 onwards](https://www.mcxindia.com/docs/default-source/products/contract-specification/natural-gas-mini-option/natural-gas-mini-%28250-mmbtu%29-options-march-2026-contract-onwards.pdf).

Published March 2027 Crude Oil futures and January 2027 Natural Gas futures
specifications were also checked on the MCX product pages; their physical
quantities and per-unit quotations remain the same. A future contract-size or
quotation change must be reviewed before expanding this allowlist.
