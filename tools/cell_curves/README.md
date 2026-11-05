# Cell curves: where they come from

`cells/profiles.json` holds one resting-voltage curve per cell type: SOC 0–100 % in 5 % steps → cell voltage. This folder has the script that digitized them. It isn't used by the app at run time.

## Sources, in order of preference

1. **Manufacturer datasheet, 0.2C discharge chart** (Molicel P30B, M35A, P45B, P50B; Murata VTC5D; Samsung 50S). 0.2C is the lightest load makers chart; it reads about 10–15 mV below resting.
2. **Independent lab test at a lighter load**, where the maker charts nothing that light:
   - lygte-info.dk 0.2 A discharge (the top curve of each "Discharge, capacity" chart): Sony/Murata VTC6 and VTC6A, Samsung 25R, 30Q and 40T, LG HG2, Molicel P28A and P42A. 0.2 A is C/12–C/25 for these cells, within ~10 mV of resting.
   - About:Energy C/30 discharge for the Reliance RS50 (Reliance's spec has rate tables but no curve).

Each curve's `source` in profiles.json names the document or page, the test conditions and the capacity used.

## Method

- Take the discharge curve from full (CC-CV to 4.2 V) to 2.8 V. SOC = 1 − Ah out ÷ Ah to 2.8 V.
- Read the voltage at every 5 % of that capacity. Datasheet lines are several pixels thick, so `envelope_xy` uses the middle of the line; lygte charts are 1 px. Both force the voltage to be non-increasing as charge is taken out.
- Set 0 % = 2.80 V (the cut-off) and 100 % = 4.20 V (the rated charge voltage; the first points of a discharge already sit under load).
- Check each digitization by drawing the sampled points back over the chart.

`fm_gt_original` is the average of the 40T and VTC6A curves. `generic_nmc` ("Other NMC/NCA") is the point-by-point median of all the measured curves and is marked `verified: false`.

## Limits

- New cells at about 25 °C. Ageing and temperature shift the curve.
- The BMS stops at Charge End while charge current flows, when cells read roughly 0.03–0.05 V above resting, so the pack settles a little below the chosen percentage.
- LiFePO4 (Onewheel V1/Plus, A123 ANR26650) isn't supported. Its curve is flat from about 20 to 90 %, so a voltage limit can't pick a percentage.

## Usage

```
python3 tools/cell_curves/digitize.py "<lygte Capacity chart>.png" <Ah per vertical gridline>
```

For datasheet charts, call `envelope_xy(path, box, xcal, ycal, is_curve, exclude)` with two known pixel/value pairs per axis and a colour test for the curve, then `soc_table(points, capacity)`.
