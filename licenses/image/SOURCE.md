# Corresponding source

This Mendari container image contains software licensed under the GNU General Public License
and the GNU Lesser General Public License (Debian's packages, and libraries bundled inside the
psycopg-binary package), and other software whose licence asks for its source to be available.
Its complete corresponding source, at the exact versions in this image, is published in the same
registry repository as the image, under the image's tag followed by `-sources`. For

    ghcr.io/<owner>/mendari-engine:v1.2.3

it is

    ghcr.io/<owner>/mendari-engine:v1.2.3-sources

The `-sources` image holds files only, under `/sources`. To copy them out:

    docker create --name mendari-sources ghcr.io/<owner>/mendari-engine:v1.2.3-sources none
    docker cp mendari-sources:/sources ./sources
    docker rm mendari-sources

What it holds (its `/sources/README.md` lists every file with its SHA-256):

- `debian/`: the Debian source package (`.dsc` and the files it names) of every Debian package
  installed in the image, at the version installed.
- `components/`: the source of the other GPL and LGPL software, as released by its authors at
  the version in the image: psycopg (LGPL-3.0) with psycopg-binary's C implementation, the
  libraries psycopg-binary bundles under the LGPL (libxcrypt and keyutils, with the build
  scripts that compiled libxcrypt), CPython (with the official image's Dockerfile that compiled
  it), pip and certifi.
- `sbom/`: the image's software bill of materials (SPDX), one per architecture, with every
  package's source location.

/licenses/THIRD_PARTY_NOTICES.md lists every component in the image with its licence and where
its source is; /licenses/inventory.json lists the same, and what the `-sources` image was made
from.

The `-sources` image stays published for as long as the image it belongs to. If you cannot get
it, open an issue in the Mendari repository (https://github.com/Junebugg1214/mendari) and the
maintainers will send it to you.
