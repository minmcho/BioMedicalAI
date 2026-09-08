# Forward Deployment 01 — Production Python

## Goal
Move from research/notebook Python to maintainable software suitable for deployment in biomedical environments.

## Skills

- Project structure
- Type hints
- Dataclasses
- Configuration management
- Logging
- Exceptions
- Input validation
- Unit and integration testing
- Packaging
- Git workflow
- Documentation

## Hands-on Project — Biomedical Sequence Service Core

Refactor the Week 1 DNA toolkit into a production-style Python package.

Required structure:

```text
src/
  biomedical_sequence/
    __init__.py
    dna.py
    models.py
    validation.py
    logging_config.py
tests/
README.md
pyproject.toml
```

The package should expose typed functions for sequence validation, GC content, reverse complement, transcription, and translation.

## Engineering requirements

- No notebook dependency in production code.
- Use type hints throughout.
- Raise explicit exceptions for invalid input.
- Add structured logging for failures and important operations.
- Achieve meaningful unit-test coverage.
- Document public functions.
- Separate domain logic from I/O.

## Forward Deployment mindset

Assume a biomedical customer will integrate this package into a larger system. Your code must be predictable, testable, observable, and easy for another engineer to operate.

## Deliverables

1. Production package
2. Test suite
3. API documentation in README
4. Design decisions document
5. Test/coverage report
6. Short reflection: research code vs production code
