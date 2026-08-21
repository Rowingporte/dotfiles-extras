#!/usr/bin/env python3
"""
Point every source in spec-pkg-tools/pkg/source/*.yml at local files instead
of the network, so `make` builds fully offline.

Only touches YAML files under <repo>/spec-pkg-tools/pkg/source/. Nothing
else in the repository is modified.

Two kinds of sources:
  - "archive" packages (binutils, gcc-13.4.0/14.3.0, gdb, gmp, mpfr, mpc,
    isl, expat, dtc, qemu, sis, rtems-tools, newlib): each yml has an
    `archive-file:` (expected local name) and `archive-url:` (remote
    location(s)). This script rewrites `archive-url:` to a local
    `file://<archives-dir>/<name>` URL matching that name.
  - "repository" package (gcc.yml only): has a `directory:` field that is
    passed straight to `git clone`/`git fetch` as the remote. This script
    rewrites it to a local `file://<gcc-mirror-path>` bare repository.
    Because the value itself becomes a file:// path, no git insteadOf/
    global config is needed - git never has to rewrite anything.

Usage:
    ./configure-offline-sources.py \\
        --repo /path/to/rtems-eb \\
        --archives /path/to/rtems-eb/src \\
        --gcc-mirror /path/to/gcc-mirror.git

Defaults: --repo is auto-detected as this script's directory, or whichever
single subdirectory next to it contains a spec-pkg-tools/ folder (works
regardless of what the clone is named - rtems-eb, rtems, whatever).
--archives defaults to <repo>/src, --gcc-mirror to <repo's parent>/gcc-mirror.git.
Safe to re-run (idempotent).
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ARCHIVE_FILE_RE = re.compile(r'^archive-file:.*/([^/\n]+)\s*$', re.MULTILINE)

# Matches `archive-url:` followed either by an inline value on the same
# line, or by a following block of `- ...` list items (each possibly
# indented). Captures the whole field so it can be replaced wholesale.
ARCHIVE_URL_BLOCK_RE = re.compile(
    r'^archive-url:[^\n]*\n(?:^-[^\n]*\n|^[ \t]+-[^\n]*\n)*', re.MULTILINE)

DIRECTORY_RE = re.compile(r'^directory:.*$', re.MULTILINE)
COMMIT_RE = re.compile(r'^commit:\s*(\S+)\s*$', re.MULTILINE)
ORIGIN_COMMIT_RE = re.compile(r'^origin-commit:\s*(\S+)\s*$', re.MULTILINE)


def has_commit(git_dir: Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(git_dir), "cat-file", "-e", commit],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def find_repo_root(script_dir: Path) -> Path:
    if (script_dir / "spec-pkg-tools").is_dir():
        return script_dir
    candidates = [p for p in script_dir.iterdir()
                  if p.is_dir() and (p / "spec-pkg-tools").is_dir()]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        sys.exit(f"error: multiple rtems clones found next to the script "
                  f"({names}) - pass --repo explicitly")
    sys.exit(f"error: no rtems clone with spec-pkg-tools/ found in "
              f"{script_dir} or its immediate subdirectories - pass "
              f"--repo explicitly")


def patch_archive_yml(yml_path: Path, archives_dir: Path) -> None:
    text = yml_path.read_text()
    match = ARCHIVE_FILE_RE.search(text)
    if not match:
        print(f"  skip {yml_path.name}: no archive-file field found")
        return
    basename = match.group(1)
    local_file = archives_dir / basename
    if not local_file.exists():
        print(f"  WARNING {yml_path.name}: expected archive not found at "
              f"{local_file}")
    new_url_field = f"archive-url: file://{local_file}\n"
    new_text, count = ARCHIVE_URL_BLOCK_RE.subn(new_url_field, text, count=1)
    if count == 0:
        print(f"  skip {yml_path.name}: no archive-url field found")
        return
    yml_path.write_text(new_text)
    print(f"  patched {yml_path.name}: archive-url -> file://{local_file}")


def patch_repository_yml(yml_path: Path, gcc_mirror: Path) -> None:
    text = yml_path.read_text()
    if not gcc_mirror.exists():
        print(f"  WARNING {yml_path.name}: gcc mirror not found at "
              f"{gcc_mirror}")
    else:
        for label, regex in (("commit", COMMIT_RE),
                              ("origin-commit", ORIGIN_COMMIT_RE)):
            match = regex.search(text)
            if not match:
                continue
            sha = match.group(1)
            if has_commit(gcc_mirror, sha):
                print(f"  {label} {sha[:12]}: present in {gcc_mirror.name}")
            else:
                print(f"  WARNING {label} {sha} is NOT in {gcc_mirror}: "
                      f"the offline build will fail on this source until "
                      f"that exact commit is fetched into the mirror while "
                      f"you still have network access")
    new_text, count = DIRECTORY_RE.subn(f"directory: file://{gcc_mirror}",
                                        text, count=1)
    if count == 0:
        print(f"  skip {yml_path.name}: no directory field found")
        return
    yml_path.write_text(new_text)
    print(f"  patched {yml_path.name}: directory -> file://{gcc_mirror}")


def is_git_repo(path: Path) -> bool:
    """True for a bare repo (HEAD/objects/refs at its root) or a normal
    clone (has a .git subdirectory) - either works as a `git clone`/`git
    fetch` source, whatever the containing folder happens to be named."""
    if not path.is_dir():
        return False
    if (path / "HEAD").exists() and (path / "objects").is_dir() \
            and (path / "refs").is_dir():
        return True
    return (path / ".git").is_dir()


def find_git_repos(search_dir: Path, max_depth: int = 2) -> list[Path]:
    """Search search_dir and its subdirectories (up to max_depth levels)
    for anything that is itself a git repo. Manually fetched/cloned
    mirrors are often nested one or two levels deep in an unpredictable
    folder, e.g. gcc.git/gcc-13.2.0/ where gcc.git itself is just a plain
    container folder, not a repo."""
    found = []
    if is_git_repo(search_dir):
        found.append(search_dir)
        return found  # a repo's own subdirectories aren't separate repos
    if max_depth <= 0 or not search_dir.is_dir():
        return found
    for child in sorted(search_dir.iterdir()):
        if child.is_dir():
            found.extend(find_git_repos(child, max_depth - 1))
    return found


def find_gcc_mirror(search_dir: Path) -> Path:
    candidates = find_git_repos(search_dir)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = ", ".join(str(c.relative_to(search_dir)) for c in candidates)
        sys.exit(f"error: multiple git repos found under {search_dir} "
                  f"({names}) - pass --gcc-mirror explicitly")
    sys.exit(f"error: no git repo (gcc mirror) found under {search_dir} "
              f"(searched 2 levels deep) - pass --gcc-mirror explicitly")


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path,
                       default=find_repo_root(script_dir),
                       help="path to the rtems-eb clone")
    parser.add_argument("--archives", type=Path, default=None,
                       help="directory holding the downloaded source "
                       "archives (default: <repo>/src)")
    parser.add_argument("--gcc-mirror", type=Path, default=None,
                       help="path to the local gcc bare mirror "
                       "(default: auto-detected bare repo next to --repo)")
    args = parser.parse_args()

    repo = args.repo.resolve()
    archives_dir = (args.archives or (repo / "src")).resolve()
    gcc_mirror = (args.gcc_mirror or find_gcc_mirror(repo.parent)).resolve()
    source_dir = repo / "spec-pkg-tools" / "pkg" / "source"

    if not source_dir.is_dir():
        sys.exit(f"error: {source_dir} not found")

    print(f"repo:       {repo}")
    print(f"archives:   {archives_dir}")
    print(f"gcc mirror: {gcc_mirror}")
    print()

    for yml_path in sorted(source_dir.glob("*.yml")):
        text = yml_path.read_text()
        if "workspace-type: repository" in text:
            patch_repository_yml(yml_path, gcc_mirror)
        elif "workspace-type: archive" in text:
            patch_archive_yml(yml_path, archives_dir)
        else:
            print(f"  skip {yml_path.name}: unknown workspace-type")

    print("\ndone. Only files under spec-pkg-tools/pkg/source/ were changed.")


if __name__ == "__main__":
    main()
