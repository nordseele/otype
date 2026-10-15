#!/usr/bin/env python3
"""Build otype-<version>.vsix from this folder without vsce or npm.

A .vsix is a zip: [Content_Types].xml, extension.vsixmanifest and the
extension's files under extension/.  Install it with
    code --install-extension otype-<version>.vsix
or, in VS Code, "Extensions: Install from VSIX...".
"""
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {"__pycache__", "test", ".git"}
SKIP_FILES = {"package_vsix.py"}

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
{defaults}
</Types>
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Tags></Tags>
    <Categories>Programming Languages</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace"/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
  </Assets>
</PackageManifest>
"""

TYPES = {".json": "application/json", ".js": "application/javascript",
         ".md": "text/markdown", ".py": "text/plain", ".sh": "text/plain",
         ".oty": "text/plain", ".OTY": "text/plain", ".txt": "text/plain",
         ".vsixmanifest": "text/xml"}


def files():
    for root, dirs, names in os.walk(HERE):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in sorted(names):
            if n in SKIP_FILES or n.endswith(".vsix") or n.endswith(".pyc"):
                continue
            full = os.path.join(root, n)
            yield full, os.path.relpath(full, HERE).replace(os.sep, "/")


def main():
    pkg = json.load(open(os.path.join(HERE, "package.json")))
    out = os.path.join(HERE, f"{pkg['name']}-{pkg['version']}.vsix")
    exts = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for full, rel in files():
            z.write(full, "extension/" + rel)
            exts.add(os.path.splitext(rel)[1])
        z.writestr("extension.vsixmanifest", MANIFEST.format(
            name=pkg["name"], version=pkg["version"], publisher=pkg["publisher"],
            display=pkg.get("displayName", pkg["name"]),
            description=pkg.get("description", ""),
            engine=pkg["engines"]["vscode"]))
        exts.add(".vsixmanifest")
        defaults = "\n".join(
            f'  <Default Extension="{e}" ContentType="{TYPES.get(e, "application/octet-stream")}"/>'
            for e in sorted(x for x in exts if x))
        z.writestr("[Content_Types].xml", CONTENT_TYPES.format(defaults=defaults))
    print(out)


if __name__ == "__main__":
    sys.exit(main())
