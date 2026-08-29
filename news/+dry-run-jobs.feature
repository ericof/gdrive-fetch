`--dry-run` now honours `-j`: that many files are compared against the output
folder at once, so md5 verification no longer runs one file at a time. The
report is still ordered by destination path, whatever order the comparisons
finish in.
