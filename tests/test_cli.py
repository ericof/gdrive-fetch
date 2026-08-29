from gdrive_fetch.cli import extract_id

import pytest


@pytest.mark.parametrize(
    "value",
    [
        "1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz?usp=sharing",
        "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view",
        "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz",
    ],
)
def test_extract_id(value: str) -> None:
    assert extract_id(value) == "1AbCdEfGhIjKlMnOpQrStUvWxYz"
