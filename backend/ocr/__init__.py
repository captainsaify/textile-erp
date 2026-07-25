"""OCR pipeline -- docs/07_OCR.md.

Deliberately independent of FastAPI/Celery/SQLAlchemy so every stage is
unit-testable against fixture images without standing up the app
(docs/01_Architecture.md §3). The DB-facing glue (attachments, learning
dictionary, draft construction) lives in backend/services/ocr_service.py.
"""
