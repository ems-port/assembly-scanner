<div align="center">

# Assembly Scanner

**Scan a component bag. Find its placements. Keep building.**

An open-source macOS desktop tool that reads a Mouser part-number barcode, matches it to
your bill of materials, and highlights the corresponding designators in Autodesk Fusion
Electronics.

[![Release](https://img.shields.io/badge/release-0.2.0-blue)](https://github.com/ems-port/assembly-scanner/releases)
[![Platform](https://img.shields.io/badge/platform-macOS%2013%2B-lightgrey)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

---

## At a glance

1. Open an Electronics board in Fusion and load your BOM.
2. Hold a component bag label in front of the camera.
3. Assembly Scanner reads the barcode, looks up the part, and selects its designators in
   Fusion's schematic. Fusion cross-probes the selection onto the board.

The BOM is the source of designators. The scanner does not infer placements from the label,
and it leaves Fusion's design data unchanged.

## Features

- Reads Code 128 barcodes on Mouser component labels.
- Imports `.xlsx` workbooks and `.csv` files; recognizes designator and part-number columns.
- Handles fitted parts, no-placement purchasing lines, designator ranges, and multi-sheet BOMs.
- Chooses an open Fusion board using BOM placement coverage, with a manual board override.
- Shows the decoded part, manufacturer number, description, designators, board status, and
  Fusion outcome as readable, selectable text; copies the result as plain text.
- Mirrors the camera image on request, and pauses capture, decoding, and MCP polling while
  paused or while the app is in the background.
- Reconnects its MCP session after a Fusion server restart and backs off after repeated
  recursion errors to avoid retry loops.

## Requirements

- macOS 13 or later.
- Autodesk Fusion with the Fusion MCP server enabled and listening locally at
  `http://127.0.0.1:27182/mcp`.
- A camera that can focus on a small printed barcode. A Continuity Camera or autofocus
  webcam usually works better than a fixed-focus display camera.
- A BOM workbook or CSV containing the relevant part numbers and designators.

Assembly Scanner uses the local MCP endpoint only. It does not send images, barcodes, BOMs,
or Fusion data to a cloud service.

## Install

Download or clone this repository, then run the installer from Terminal:

```bash
git clone https://github.com/ems-port/assembly-scanner.git
cd assembly-scanner
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
./launcher/install.sh
```

The installer places **Assembly Scanner.app** in `~/Applications` and creates a Desktop
alias. Open the app and grant camera access when macOS asks. The app bundle is ad-hoc signed
for a stable macOS camera-permission identity; it is not Developer ID signed or notarized.

To update an existing install, pull the latest source and run `./launcher/install.sh` again.
To remove the installed app and Desktop alias, run:

```bash
./launcher/install.sh --uninstall
```

## Use

1. Open the target board in Fusion Electronics. If several boards are open, Assembly Scanner
   selects the board whose designators best match the loaded BOM. You can choose a board
   manually from the **Board** menu for the current session.
2. Launch Assembly Scanner, click **Load BOM…**, and choose your workbook or CSV.
3. Hold a component bag about 15–25 cm from the camera, level and still, inside the dashed
   guide. The part result and Fusion selection remain after you lower the bag.
4. Use **Pause** while waiting, **Invert image** to mirror the camera view, **Rescan boards**
   after changing open Fusion documents, and **Copy result** to copy the current result.

The displayed `sum: N` appears after the designator list when a part maps to more than three
placements.

### Result meanings

| Display | Meaning | Fusion action |
|---|---|---|
| Green part number and designators | Part matched a fitted BOM line | Selects the matching schematic components; Fusion cross-probes to the board |
| `NO PLACEMENT FOUND` | Part is in the BOM but has no fitted designators | Leaves the current selection alone |
| `NOT IN CURRENT BOM` | A part-number barcode was read but is absent from this BOM | Leaves the current selection alone |
| `NOT ON BOARD` | Some BOM designators were not found on the chosen board | Reports missing designators; reports the remaining selection |
| `LABEL SEEN` | Other barcodes were read, but the part-number barcode was not | Adjust focus, angle, distance, or lighting |
| Fusion connection error | The local MCP server did not return a usable response | See [Fusion MCP troubleshooting](docs/FUSION_MCP_TROUBLESHOOTING.md) |

## Fusion MCP troubleshooting

The app talks to Fusion over MCP at `127.0.0.1:27182`. Use **Rescan boards** to request a
fresh connection and board snapshot. The command-line status check is also read-only:

```bash
.venv/bin/python -m app.fusion_client status
```

Assembly Scanner renews a lost MCP session and retries once. It uses Fusion's structured
read tools for board data and keeps script output quiet for selection actions. After three
identical recursion errors, automatic polling backs off until **Rescan boards** is clicked.
The [troubleshooting guide](docs/FUSION_MCP_TROUBLESHOOTING.md) documents the observed
failure, diagnostics, and recovery steps.

## Development

Run from the repository while developing:

```bash
.venv/bin/python -m app.main
```

To inspect the Fusion link without opening the camera:

```bash
.venv/bin/python -m app.fusion_client status
.venv/bin/python -m app.fusion_client boards
.venv/bin/python -m app.fusion_client designators
```

The application is organized into a camera capture/decode path, a BOM importer and resolver,
a Qt interface, and a Fusion MCP client. See [Architecture](docs/ARCHITECTURE.md) for the
component boundaries and operational behavior.

Version history and the diagnostic summary are in [CHANGELOG.md](CHANGELOG.md).

## Limitations

- Barcode readability depends on focus, label condition, lighting, angle, and distance.
- Fusion highlight behavior depends on Autodesk's Fusion MCP server and Fusion's schematic
  cross-probe behavior.
- The MCP protocol and Fusion tools can change between Fusion releases; compatibility should
  be checked after Fusion updates.
- A small set of bench scenarios has been demonstrated. The full acceptance procedure has
  not been completed on every supported camera and Fusion configuration.
- This project is a practical workshop tool, not a placement-verification or safety system.

## Contributing

Bug reports and pull requests are welcome. Please include your macOS version, Fusion version,
the exact visible error, and the output of the read-only `status` command. Remove project BOMs,
personal photos, account details, and other private material before attaching logs or files.

## License

Distributed under the MIT License. See [LICENSE](LICENSE).
