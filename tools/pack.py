"""Pack a staged folder into a .zip or .tar.gz for a release.

    python3 -I tools/pack.py <folder> <out.zip | out.tar.gz>

The folder becomes the archive's top level. Entries are sorted, owners are
root (tar headers would otherwise carry the builder's user name), modes are
755 for folders and executables and 644 for the rest, and symlinks are kept.
Set SOURCE_DATE_EPOCH to give every entry the same timestamp.
"""

import os
import stat
import sys
import tarfile
import time
import zipfile


def entries(top):
    base = os.path.basename(os.path.normpath(top))
    out = [(top, base)]
    for dirpath, dirnames, files in os.walk(top):
        dirnames.sort()
        for name in dirnames + sorted(files):
            p = os.path.join(dirpath, name)
            out.append((p, base + "/" + os.path.relpath(p, top).replace(os.sep, "/")))
    return out


def mode_of(st):
    if stat.S_ISDIR(st.st_mode) or st.st_mode & stat.S_IXUSR:
        return 0o755
    return 0o644


def main(top, out):
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    items = entries(top)
    tmp = out + ".part"
    if out.endswith(".zip"):
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for path, arc in items:
                st = os.lstat(path)
                t = time.localtime(int(epoch) if epoch else st.st_mtime)
                info = zipfile.ZipInfo(arc + ("/" if stat.S_ISDIR(st.st_mode) else ""),
                                       date_time=max(t[:6], (1980, 1, 1, 0, 0, 0)))
                info.create_system = 3  # Unix, so unzip keeps the modes below
                if stat.S_ISLNK(st.st_mode):
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(info, os.readlink(path))
                elif stat.S_ISDIR(st.st_mode):
                    info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                    z.writestr(info, b"")
                else:
                    info.external_attr = (stat.S_IFREG | mode_of(st)) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with open(path, "rb") as f:
                        z.writestr(info, f.read())
    elif out.endswith(".tar.gz"):
        def clean(ti):
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            if not ti.issym():
                ti.mode = 0o755 if ti.isdir() or ti.mode & stat.S_IXUSR else 0o644
            if epoch:
                ti.mtime = int(epoch)
            return ti
        with tarfile.open(tmp, "w:gz", format=tarfile.PAX_FORMAT) as t:
            for path, arc in items:
                t.add(path, arc, recursive=False, filter=clean)
    else:
        sys.exit("pack.py: output must end in .zip or .tar.gz")
    os.replace(tmp, out)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
