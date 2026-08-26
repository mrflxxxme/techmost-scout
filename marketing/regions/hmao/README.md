# ХМАО-Югра: заход через регионального партнёра

Два адресата, два трека:

1. **АО «Югорский рыбоводный завод»** (ugrafish.ru, Ханты-Мансийск) — бизнес.
   Предложение: оплачиваемый пилот «диагностика + подбор» под технологическую
   задачу завода.
2. **Депнедра и природных ресурсов Югры** (в быту «минприроды ХМАО») —
   власть. Предложение: соглашение о сотрудничестве + представление
   предприятиям-природопользователям. Денег у бюджета не просим.

Заход — через регионального партнёра-посредника (модель свердловского кейса).
Материалы двухконтурные: показываемые (боли сформулированы как вызовы
отрасли/региона) и внутренняя записка посреднику (прямые формулировки, вилки
цен, возражения). В показываемых материалах цен нет; партнёр в слайдах не
упоминается.

## Материалы

| Файл | Что это |
|---|---|
| [research-ugrafish-2026-08-26.md](research-ugrafish-2026-08-26.md) | Разведка по заводу: юрлицо, деятельность, финансы, болевые точки, гипотезы решений |
| [research-depnedra-2026-08-26.md](research-depnedra-2026-08-26.md) | Разведка по ведомству: точное название, руководство, программы, KPI, болевые точки |
| [research-market-2026-08-26.md](research-market-2026-08-26.md) | Статистическая подложка: инновационная среда Югры + аквакультура и природопользование |
| [deck-texts.md](deck-texts.md) | Тексты слайдов обоих деков — для правок; правьте здесь, затем переносите в генератор |
| [gen_hmao_decks.py](gen_hmao_decks.py) | Генератор обоих деков (python-pptx, дизайн v3 — Р-29) |
| [gen_hmao_onepagers.py](gen_hmao_onepagers.py) | Генератор справок на лист (python-docx) |
| [sitall-deck-urz.pptx](sitall-deck-urz.pptx) / [PDF](sitall-deck-urz.pdf) | Дек для Югорского рыбоводного завода, 10 слайдов |
| [sitall-deck-depnedra.pptx](sitall-deck-depnedra.pptx) / [PDF](sitall-deck-depnedra.pdf) | Дек для Депнедра Югры, 10 слайдов |
| [sitall-spravka-urz.docx](sitall-spravka-urz.docx) / [PDF](sitall-spravka-urz.pdf) | Справка на лист для завода |
| [sitall-spravka-depnedra.docx](sitall-spravka-depnedra.docx) / [PDF](sitall-spravka-depnedra.pdf) | Справка на лист для ведомства |
| [note-broker.md](note-broker.md) | Внутренняя записка посреднику: не передавать адресатам |
| [sitall-note-broker.docx](sitall-note-broker.docx) | Она же в DOCX; пересборка — [gen_hmao_note_docx.py](gen_hmao_note_docx.py) |
| [log.md](log.md) | Журнал контактов и договорённостей |

Пересборка: `node gen-hmao-decks.js && python3 gen_hmao_onepagers.py &&
python3 gen_hmao_note_docx.py`
(нужны `python-pptx`, `python-docx`; PDF — через LibreOffice:
`soffice --headless --convert-to pdf <файл>`). На машине без Segoe UI PDF
получит подменённый шрифт — итоговые PDF для рассылки экспортируйте из
PowerPoint/Word.
