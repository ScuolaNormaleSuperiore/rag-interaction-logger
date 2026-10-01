## Tests

The test suite is split by runtime dependency:

```bash
python run-tests.py --unit          # pure Python; no Cheshire Cat required
python run-tests.py --integration   # hook adapters; requires the running Cat container
python run-tests.py                 # unit and integration tests in the container
python run-tests.py --detailed      # same suite, listing each test
```

Unit tests cover pure record assembly, metadata and release packaging.
Integration tests cover Cheshire Cat hook registration, priorities and the
observer contract. The integration suite is skipped on a local interpreter
where Cheshire Cat is unavailable.
