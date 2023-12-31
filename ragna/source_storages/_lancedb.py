import uuid

from ragna.core import Document, PackageRequirement, Requirement, Source, SourceStorage
from ragna.utils import chunk_pages, page_numbers_to_str, take_sources_up_to_max_tokens


class LanceDB(SourceStorage):
    @classmethod
    def requirements(cls) -> list[Requirement]:
        return [
            PackageRequirement("lancedb>=0.2"),
            PackageRequirement(
                "pyarrow",
                # See https://github.com/apache/arrow/issues/38167
                exclude_modules=["__dummy__"],
            ),
            PackageRequirement("sentence-transformers"),
        ]

    def __init__(self, config):
        super().__init__(config)

        import lancedb
        import pyarrow as pa
        from sentence_transformers import SentenceTransformer

        self._db = lancedb.connect(config.local_cache_root / "lancedb")
        self._model = SentenceTransformer("paraphrase-albert-small-v2")
        self._schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("document_id", pa.string()),
                pa.field("page_numbers", pa.string()),
                pa.field("text", pa.string()),
                pa.field(
                    self._VECTOR_COLUMN_NAME,
                    pa.list_(pa.float32(), self._model[-1].word_embedding_dimension),
                ),
                pa.field("num_tokens", pa.int32()),
            ]
        )

    def _embed(self, batch):
        return [self._model.encode(sentence) for sentence in batch]

    _VECTOR_COLUMN_NAME = "embedded_text"

    def store(
        self,
        documents: list[Document],
        *,
        chat_id: uuid.UUID,
        chunk_size: int = 500,
        chunk_overlap: int = 250,
    ) -> None:
        table = self._db.create_table(name=str(chat_id), schema=self._schema)

        for document in documents:
            for chunk in chunk_pages(
                document.extract_pages(),
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                tokenizer=self._model.tokenizer,
            ):
                table.add(
                    [
                        {
                            "id": str(uuid.uuid4()),
                            "document_id": str(document.id),
                            "page_numbers": page_numbers_to_str(chunk.page_numbers),
                            "text": chunk.text,
                            self._VECTOR_COLUMN_NAME: self._model.encode(chunk.text),
                            "num_tokens": chunk.num_tokens,
                        }
                    ]
                )

    def retrieve(
        self,
        documents: list[Document],
        prompt: str,
        *,
        chat_id: uuid.UUID,
        chunk_size: int = 500,
        num_tokens: int = 1024,
    ) -> list[Source]:
        table = self._db.open_table(str(chat_id))

        # We cannot retrieve source by a maximum number of tokens. Thus, we estimate how
        # many sources we have to query. We overestimate by a factor of two to avoid
        # retrieving to few sources and needed to query again.
        limit = int(num_tokens * 2 / chunk_size)
        results = (
            table.search(vector_column_name=self._VECTOR_COLUMN_NAME)
            .limit(limit)
            .to_arrow()
        )

        document_map = {str(document.id): document for document in documents}
        return list(
            take_sources_up_to_max_tokens(
                (
                    Source(
                        id=result["id"],
                        document=document_map[result["document_id"]],
                        # For some reason adding an empty string during store() results
                        # in this field being None. Thus, we need to parse it back here.
                        # TODO: See if there is a configuration option for this
                        location=result["page_numbers"] or "",
                        content=result["text"],
                        num_tokens=result["num_tokens"],
                    )
                    for result in results.to_pylist()
                ),
                max_tokens=num_tokens,
            )
        )
