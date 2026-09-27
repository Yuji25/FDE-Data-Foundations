# Completed classroom challenges

The three completed, executed notebooks are in `challenges/notebooks/`. They use only this repository's `data/raw/`, the existing `src/pipeline` functions and the local `mock_api`.

From the repository root, run:

```bash
PYTHONPATH=src .venv/bin/python -m pipeline.main --run-date 2026-09-27 --start-mock
.venv/bin/jupyter execute challenges/notebooks/FlashEats_Class5_Student.ipynb
.venv/bin/jupyter execute challenges/notebooks/FlashEats_Class6_Student.ipynb
.venv/bin/jupyter execute challenges/notebooks/FlashEats_Class7_Challenge.ipynb
```

The first command rebuilds the canonical WARN-gated evidence and preserves eight successful Dispatch pages. Execute notebooks from the repository root so local imports and paths resolve. Class 5 starts and terminates its own mock API child; port 8000 must be free. Important outputs are retained in each notebook. Consolidated answers and limitations are in `challenges/ANSWERS.md`.
