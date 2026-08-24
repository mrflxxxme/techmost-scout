-- ALTER-команды для живой базы data/scout.db (выполняет оркестратор при слиянии, урок Э2-Ж:
-- перед выполнением снять резервную копию файла базы; NOT NULL без DEFAULT на непустой таблице
-- валит INSERT — здесь это неактуально, обе таблицы получают DEFAULT/NULL безопасно).
--
-- requests.report_json — nullable, безопасно на непустой таблице (4 строки на 24.08.2026):
-- существующие строки получат NULL, колонка Mapped[str | None] это и ожидает.
ALTER TABLE requests ADD COLUMN report_json TEXT;

-- briefs.owner_answers — таблица briefs пуста (0 строк на 24.08.2026, РЕШ-156), поэтому
-- NOT NULL с DEFAULT безопасен и на будущих строках, вставленных в обход ORM.
ALTER TABLE briefs ADD COLUMN owner_answers JSON NOT NULL DEFAULT '[]';

-- Проверено на копии живой базы (не на оригинале) командой sqlite3 — pragma table_info
-- после ALTER совпадает с ожидаемой схемой моделей (report_json TEXT nullable=1 позиция 7;
-- owner_answers JSON notnull=1 default '[]', позиция 12 — ALTER всегда добавляет колонку в
-- конец таблицы, это отличается от порядка полей в models.py, но на работу ORM не влияет:
-- SQLAlchemy адресует колонки по имени, не по позиции).
