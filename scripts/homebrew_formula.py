#!/usr/bin/env python3
"""Generate the Homebrew formula for museletter at a given version.

Usage: homebrew_formula.py <version> <output.rb>

Resolves museletter's dependency tree from PyPI (retrying until the version is
published, since the index lags right after a release) and writes a
Language::Python::Virtualenv formula. The release workflow runs this to update
the tap; it is safe to run locally too. Requires `uv` on PATH.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request


def sdist(name: str, version: str) -> tuple[str, str]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json") as resp:
        data = json.load(resp)
    for url in data["urls"]:
        if url["packagetype"] == "sdist":
            return url["url"], url["digests"]["sha256"]
    raise SystemExit(f"no sdist published for {name} {version}")


def main() -> None:
    version, out_path = sys.argv[1], sys.argv[2]
    venv = tempfile.mkdtemp()
    subprocess.run(["uv", "venv", venv, "-q"], check=True)
    py = os.path.join(venv, "bin", "python")

    for _ in range(20):  # PyPI's index can lag for a minute after publish
        if subprocess.run(["uv", "pip", "install", "--python", py, f"museletter=={version}", "-q"]).returncode == 0:
            break
        time.sleep(15)
    else:
        raise SystemExit(f"museletter=={version} did not become resolvable on PyPI")

    freeze = subprocess.run(
        ["uv", "pip", "freeze", "--python", py], capture_output=True, text=True, check=True
    ).stdout
    pins = dict(line.split("==", 1) for line in freeze.splitlines() if "==" in line)

    mu_url, mu_sha = sdist("museletter", version)
    blocks = []
    for name in sorted((n for n in pins if n.lower() != "museletter"), key=str.lower):
        url, sha = sdist(name, pins[name])
        blocks.append(f'  resource "{name}" do\n    url "{url}"\n    sha256 "{sha}"\n  end\n')
    resources = "\n".join(blocks)

    formula = f'''class Museletter < Formula
  include Language::Python::Virtualenv

  desc "Headless, agent-first newsletter engine using SQLite and Amazon SES"
  homepage "https://github.com/sanketsaurav/museletter"
  url "{mu_url}"
  sha256 "{mu_sha}"
  license "MIT"

  depends_on "rust" => :build      # builds cryptography and pydantic-core
  depends_on "openssl@3"
  depends_on "python@3.13"

{resources}
  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "museletter #{{version}}", shell_output("#{{bin}}/museletter --version")
  end
end
'''
    with open(out_path, "w") as f:
        f.write(formula)
    print(f"wrote {out_path}: {len(blocks)} resources for museletter {version}")


if __name__ == "__main__":
    main()
