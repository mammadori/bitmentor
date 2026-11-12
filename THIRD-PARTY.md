# Third-party files and their licenses

bitmentor is under the GNU AGPLv3 ([COPYING.md](COPYING.md)). The files listed here were not written for bitmentor: they
are in the tree as their authors published them, and they keep their own licenses. Everything else in the tree was
written for this project.

| What | Where | License |
|---|---|---|
| [Leaflet](#leaflet) 1.9.4 | `static/vendor/leaflet/leaflet.js`, `leaflet.css`, `images/` | BSD-2-Clause |
| [Leaflet.markercluster](#leafletmarkercluster) 1.5.3 | `static/vendor/leaflet/leaflet.markercluster.js`, `MarkerCluster.css` | MIT |
| [QRCode for JavaScript](#qrcode-for-javascript), as modified by qrcode-terminal 0.12.0 | `static/vendor/qrcode/qrcode.js` | MIT, Apache-2.0 |
| [BIP39 English word list](#bip39-english-word-list) | `shops/bip39_english.txt` | MIT |
| [Bitcoin logo](#bitcoin-logo) | `static/bitcoin.svg` | Public domain |
| [Test vectors](#test-vectors) of NIP-44, BIP340, BIP32, BIP84, BIP173 and BIP350 | `shops/testdata/`, `shops/test_register_chain.py`, `shops/test_register_nostr.py` | Public domain, BSD-2-Clause, CC0-1.0 |

No fonts are in the tree: a community brings its own in its package (`COMMUNITY_DIR`). The icons drawn in the templates
and scripts, the logos in `docs/img/` and the example community's logo were made for this project.

## Leaflet

- **What:** the JavaScript library that draws the maps (the shop map, a shop's page, the shop form, the route page).
- **Where:** `static/vendor/leaflet/leaflet.js`, `static/vendor/leaflet/leaflet.css` and the three images in
  `static/vendor/leaflet/images/` (`marker-icon.png`, `marker-icon-2x.png`, `marker-shadow.png`).
- **Upstream:** <https://leafletjs.com>, <https://github.com/Leaflet/Leaflet>
- **Version:** 1.9.4, the files of the published `dist/`, unchanged (same bytes).
- **License:** BSD-2-Clause.

```text
BSD 2-Clause License

Copyright (c) 2010-2023, Volodymyr Agafonkin
Copyright (c) 2010-2011, CloudMade
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Leaflet.markercluster

- **What:** the Leaflet plugin that groups the shops close to each other on the shop map.
- **Where:** `static/vendor/leaflet/leaflet.markercluster.js`, `static/vendor/leaflet/MarkerCluster.css`.
- **Upstream:** <https://github.com/Leaflet/Leaflet.markercluster>
- **Version:** 1.5.3. The files carry no version: they are, byte for byte, the `dist/` files published on npm as
  1.5.3 (those of 1.5.2 are the same bytes; 1.5.1 and earlier differ).
- **License:** MIT. The published files carry no notice of their own: this is the one that travels with them.

```text
Copyright 2012 David Leaver

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## QRCode for JavaScript

- **What:** the QR code generator the register and the staff pairing page use to draw a code in the browser.
- **Where:** `static/vendor/qrcode/qrcode.js`.
- **Upstream:** QRCode for JavaScript by Kazuhiko Arase (<http://www.d-project.com/>,
  <https://github.com/kazuhikoarase/qrcode-generator>), in the form qrcode-terminal carries in its `vendor/QRCode/`
  folder, "modified to work in node" and split into modules (<https://github.com/gtanner/qrcode-terminal>).
- **Version:** qrcode-terminal 0.12.0. Changed here: its ten modules, each as published (same bytes), are joined into
  one file for the browser, with a small loader at the top and a `qrcode(text)` function at the end. The file's header
  says so.
- **License:** MIT for QRCode for JavaScript; Apache License 2.0 for qrcode-terminal, which ships no `NOTICE` file.
- The word "QR Code" is registered trademark of DENSO WAVE INCORPORATED.

QRCode for JavaScript:

```text
MIT License

Copyright (c) 2009 Kazuhiko Arase

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

qrcode-terminal:

```text
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

==============================================================
This product also include the following software:
==============================================================

 QRCode for JavaScript

 Copyright (c) 2009 Kazuhiko Arase

 URL: http://www.d-project.com/

 Licensed under the MIT license:
   http://www.opensource.org/licenses/mit-license.php

 The word "QR Code" is registered trademark of 
 DENSO WAVE INCORPORATED
   http://www.denso-wave.com/qrcode/faqpatent-e.html

Located in ./vendor/QRCode
- project has been modified to work in Node and some refactoring was done for code cleanup
```

## BIP39 English word list

- **What:** the 2048 English words of BIP39, which the site's forms use to recognize a wallet's recovery phrase typed
  into a field, and refuse it.
- **Where:** `shops/bip39_english.txt`.
- **Upstream:** <https://github.com/bitcoin/bips/blob/master/bip-0039/english.txt>; the list comes from BIP39's
  reference implementation, <https://github.com/trezor/python-mnemonic>, which carries the same file.
- **Version:** not recorded; the list is fixed. Same bytes as both upstream files (sha256
  `2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda`).
- **License:** MIT. BIP39 says "This BIP falls under the MIT License" and carries no license text of its own; this is
  the one of the reference implementation.

```text
The MIT License (MIT)

Copyright (c) 2013-2016 Pavol Rusnak

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Bitcoin logo

- **What:** the orange Bitcoin logo on the counter sign, the shop page and the customer's and till's cards.
- **Where:** `static/bitcoin.svg`.
- **Upstream:** <https://commons.wikimedia.org/wiki/File:Bitcoin.svg>, which Wikimedia Commons says is derived from
  <https://commons.wikimedia.org/wiki/File:Bitcoin_logo.svg> by Bitboy, of the Bitcoin forums. Same paths; the
  editor's metadata is dropped and an accessible name added.
- **Version:** not recorded.
- **License:** public domain, as Wikimedia Commons states for both files. No notice is required.

## Test vectors

Used only by the tests, to check the register's own code against what the specifications publish.

| What | Where | Source | Status, as the source states it |
|---|---|---|---|
| NIP-44 version 2 test vectors | `shops/testdata/nip44.vectors.json` | <https://github.com/paulmillr/nip44>, `nip44.vectors.json`; same bytes, the sha256 is the one [NIP-44](https://github.com/nostr-protocol/nips/blob/master/44.md) publishes | No license is stated for the file itself. "All NIPs are public domain" (the NIPs' README), and the repository lists its reference implementation as public domain |
| BIP340 test vectors | `shops/testdata/bip340-vectors.csv` | <https://github.com/bitcoin/bips/blob/master/bip-0340/test-vectors.csv>, same bytes | BSD-2-Clause ("This document is licensed under the 2-clause BSD license"); authors Pieter Wuille, Jonas Nick, Tim Ruffing |
| BIP32 test vectors 1 and 2 (four extended public keys) | constants in `shops/test_register_chain.py` | <https://github.com/bitcoin/bips/blob/master/bip-0032.mediawiki> | BSD-2-Clause ("This BIP is licensed under the 2-clause BSD license"); author Pieter Wuille |
| BIP84 test vector (the account key and three addresses) | constants in `shops/test_register_chain.py`; the same addresses in `shops/testdata/explorer.json` | <https://github.com/bitcoin/bips/blob/master/bip-0084.mediawiki> | CC0-1.0; author Pavol Rusnak |
| BIP173 and BIP350 test addresses | constants in `shops/test_register_chain.py` | <https://github.com/bitcoin/bips/blob/master/bip-0173.mediawiki>, <https://github.com/bitcoin/bips/blob/master/bip-0350.mediawiki> | BSD-2-Clause ("This BIP is licensed under the 2-clause BSD license"); author Pieter Wuille |
| NIP-19 example keys | constants in `shops/test_register_nostr.py` | <https://github.com/nostr-protocol/nips/blob/master/19.md> | Public domain |

The BIPs under BSD-2-Clause carry no copyright line and no license text of their own: the conditions are those of the
BSD 2-Clause License printed above for Leaflet, with the BIP's authors as the copyright holders.

`shops/testdata/explorer.json` has the shape of the answers of mempool.space's API, with made-up transactions;
`shops/testdata/register-copy-v1.json` is a register's own encrypted copy. Neither holds third-party content.

## OpenStreetMap: for whoever runs a site

Not files in the tree, but what a running site shows and uses.

- **Map tiles.** Every map loads its tiles from `tile.openstreetmap.org`, the OpenStreetMap Foundation's own servers,
  and shows "© OpenStreetMap contributors" with a link to <https://www.openstreetmap.org/copyright>. Those servers are
  run on donations and come with no guarantee: read the
  [Tile Usage Policy](https://operations.osmfoundation.org/policies/tiles/) and, for a site with many visitors, weigh
  a tile provider of your own (the address is in the four scripts of `static/` that draw a map, and in the CSP of
  `config/middleware.py`). What the visitors' browsers send there is in [Privacy](docs/privacy.md).
- **Map data.** The map data is © OpenStreetMap contributors, under the
  [Open Database License](https://opendatacommons.org/licenses/odbl/) (ODbL) 1.0. With `osm` on, the site also reads
  shops' entries from OpenStreetMap once a month and keeps what it read: that data stays under the ODbL, and the
  attribution has to stay on the pages that show it.

## Python dependencies

Not in the tree: `pip` installs them, and the published images contain them, each with its own license file. The
direct dependencies of `requirements.txt`, at the versions `requirements.lock` pins:

| Package | Version | License |
|---|---|---|
| Django | 5.2.18 | BSD-3-Clause |
| gunicorn | 23.0.0 | MIT |
| psycopg, psycopg-binary | 3.2.13 | LGPL-3.0-only |
| requests | 2.33.1 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| Markdown | 3.11.2 | BSD-3-Clause |
| whitenoise | 6.12.0 | MIT |
| coincurve | 22.0.0.dev0 (upstream commit 2d11b11) | MIT OR Apache-2.0 |
| segno | 1.6.6 | BSD-3-Clause |
| bech32 | 1.2.0 | MIT |
| websockets | 15.0.1 | BSD-3-Clause |
| cryptography | 44.0.3 | Apache-2.0 OR BSD-3-Clause |

The complete pinned set, with what these depend on, is [requirements.lock](requirements.lock). The checks image adds
tblib 3.2.2 (BSD-2-Clause, `requirements-test.lock`), never in production. All of these licenses are compatible with
the AGPLv3.
