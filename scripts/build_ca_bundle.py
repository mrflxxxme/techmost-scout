"""Сборка бандла сертификатов для GigaChat.

Зачем нужен: домены Сбера подписаны корневым сертификатом НУЦ Минцифры,
которого нет в стандартном наборе certifi. Без бандла httpx отвергает
соединение с ошибкой CERTIFICATE_VERIFY_FAILED.

Скрипт берёт сертификаты Минцифры из хранилища Windows (они попадают туда при
установке с gosuslugi.ru или вместе с обновлениями системы) и склеивает их с
набором certifi в один файл data/ru_bundle.pem.

Сам бандл в репозиторий не кладём: он собирается за секунду и зависит от машины.

Запуск:
    .venv\\Scripts\\python.exe scripts\\build_ca_bundle.py

Если сертификатов в хранилище нет: скачать «Сертификаты для Windows» с
https://www.gosuslugi.ru/crt, установить двойным щелчком и повторить запуск.
Либо передать готовые PEM-файлы: --extra путь\\к\\root.crt --extra путь\\к\\sub.crt
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "ru_bundle.pem"

#: Отпечатки сертификатов НУЦ Минцифры. Проверяем по ним, что взяли именно то,
#: что нужно, а не случайный сертификат с похожим названием.
KNOWN_THUMBPRINTS = {
    "8FF915CCAB7BC16F8C5C8099D53E0E115B3AEC2F": "Russian Trusted Root CA",
    "335D43F53451B781535FF3882DF713D3C14F8A01": "Russian Trusted Sub CA",
}

POWERSHELL_EXPORT = r"""
$found = @()
$stores = @('Cert:\LocalMachine\Root','Cert:\LocalMachine\CA','Cert:\CurrentUser\Root','Cert:\CurrentUser\CA')
foreach ($store in $stores) {
    Get-ChildItem $store -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -match 'Russian Trusted' } |
        ForEach-Object {
            $pem = "-----BEGIN CERTIFICATE-----`n" +
                   [Convert]::ToBase64String($_.RawData, 'InsertLineBreaks') +
                   "`n-----END CERTIFICATE-----"
            $encoded = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pem))
            $found += "$($_.Thumbprint)|$($_.Subject)|$encoded"
        }
}
$found | Sort-Object -Unique
"""


def certs_from_windows_store() -> list[tuple[str, str, str]]:
    """Достать сертификаты Минцифры из хранилища Windows.

    Возвращает список (отпечаток, субъект, PEM). Пустой список означает, что
    сертификаты не установлены — это не ошибка скрипта.
    """
    if sys.platform != "win32":
        return []
    try:
        # Команда фиксирована в коде, пользовательского ввода в ней нет (S603),
        # а powershell.exe берётся из PATH намеренно: путь к нему зависит от
        # разрядности и версии Windows (S607).
        result = subprocess.run(  # noqa: S603
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_EXPORT],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"Не удалось опросить хранилище Windows: {exc}")
        return []

    certs: list[tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        thumbprint, subject, pem_b64 = parts
        pem = base64.b64decode(pem_b64).decode("ascii")
        certs.append((thumbprint.upper(), subject, pem))
    return certs


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка бандла сертификатов для GigaChat")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--extra",
        type=Path,
        action="append",
        default=[],
        help="дополнительный PEM-файл (можно указать несколько раз)",
    )
    args = parser.parse_args()

    try:
        import certifi
    except ImportError:
        print("Нет пакета certifi. Установите зависимости: pip install -e .")
        return 1

    # utf-8, а не ascii: в названиях некоторых центров сертификации набора
    # certifi есть символы с диакритикой. Сами блоки PEM всегда ascii, так что
    # OpenSSL это не смущает.
    chunks = [Path(certifi.where()).read_text(encoding="utf-8")]
    added: list[str] = []

    for thumbprint, subject, pem in certs_from_windows_store():
        name = KNOWN_THUMBPRINTS.get(thumbprint)
        label = name or subject
        if name is None:
            print(f"  предупреждение: неизвестный отпечаток {thumbprint} ({subject}) — всё равно добавляю")
        if label in added:
            continue
        chunks.append(f"\n# {label} (из хранилища Windows, отпечаток {thumbprint})\n{pem}\n")
        added.append(label)

    for extra in args.extra:
        if not extra.exists():
            print(f"  файл не найден, пропускаю: {extra}")
            continue
        chunks.append(f"\n# {extra.name}\n{extra.read_text(encoding='utf-8').strip()}\n")
        added.append(extra.name)

    if not added:
        print("Сертификаты Минцифры не найдены ни в хранилище Windows, ни в --extra.")
        print("Скачайте их с https://www.gosuslugi.ru/crt, установите и повторите запуск.")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(chunks), encoding="utf-8")

    print(f"Бандл собран: {args.output} ({args.output.stat().st_size} байт)")
    print("  добавлено: " + ", ".join(added))
    print("\nПрописать в .env:")
    print(f"  GIGACHAT_CA_BUNDLE={args.output.relative_to(REPO_ROOT).as_posix()}")
    print("Проверить:  .venv\\Scripts\\scout.exe doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
