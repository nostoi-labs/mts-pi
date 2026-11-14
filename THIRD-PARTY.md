# Third-party software

## Code this project is based on

**node-carplay** — https://github.com/rhysmorgan134/node-CarPlay — MIT License.
`mtspi/carlinkit.py` and `mtspi/carlinkit_usb.py` are a Python port of its Carlinkit dongle driver
(`src/modules/DongleDriver.ts` and `src/modules/messages/`). Its license text:

> MIT License
>
> Copyright (c) Rhys Morgan and the node-CarPlay contributors
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
> documentation files (the "Software"), to deal in the Software without restriction, including without limitation
> the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
> to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of
> the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
> THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
> CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
> IN THE SOFTWARE.

## Software the installer downloads (not included in this repository)

| Software | Used for | License |
|---|---|---|
| dbus-fast | talking to BlueZ, oFono and obexd | MIT |
| PyUSB | USB link to the Carlinkit dongle | BSD-3-Clause |
| PyAV (with FFmpeg) | decoding the CarPlay video | BSD-3-Clause (FFmpeg: LGPL/GPL) |
| Pillow | drawing and JPEG encoding | MIT-CMU |
| NumPy | pixel conversion | BSD-3-Clause |
| BlueZ, oFono | Bluetooth stack and telephony, run as separate system services | GPL-2.0 |
| PipeWire, WirePlumber | audio routing, run as separate system services | MIT |
| Inter | typeface of the screens | SIL Open Font License 1.1 |

These are installed from the Raspberry Pi OS and PyPI repositories by `install.sh`, each under its own license.
