# Scripts

Reserved for thin command-line wrappers around the shared `xfed` package.
There is no pipeline runner yet. Future commands should accept a contract ID or
input/output paths and call the same stage code for all three contracts.

From the repository root, install the scaffold and run its offline checks:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

The old demo is documented separately in `legacy/README.md` and is not part of
the active pipeline. Do not use its run instructions for the new architecture.
