import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)


def _xml_to_dict(element: ET.Element, path: str = "") -> Any:
    """Convert an XML element tree to a nested dict/list structure.

    * Attributes are prefixed with ``@`` (e.g. ``@id``).
    * Text content is stored under ``#text``.
    * Repeated child tags become lists.
    """
    result: dict[str, Any] = {}

    # Attributes
    for key, val in element.attrib.items():
        result[f"@{key}"] = val

    # Children
    children: dict[str, list[Any]] = {}
    for child in element:
        child_path = f"{path}/{child.tag}" if path else child.tag
        child_data = _xml_to_dict(child, child_path)
        if child.tag not in children:
            children[child.tag] = []
        children[child.tag].append(child_data)

    for tag, items in children.items():
        if len(items) == 1:
            result[tag] = items[0]
        else:
            result[tag] = items

    # Text content
    text = (element.text or "").strip()
    if text and not result:
        return text
    if text:
        # Merge text with children: store as #text
        result["#text"] = text

    if not result:
        return text if text else ""

    return result


def _flatten_xml(element: ET.Element, separator: str = ".") -> dict[str, Any]:
    """Convert XML to a flat dict via nested conversion then JSON flattening."""
    nested = _xml_to_dict(element)
    # Reuse JSONProcessor's flattening
    from multimodal_rag.input_processing.json_processor import _flatten_json

    return _flatten_json(nested, separator=separator)


def _flatten_to_text(obj: Any, separator: str = ".") -> str:
    """Flatten any structure and render as ``key: value`` lines."""
    from multimodal_rag.input_processing.json_processor import _flatten_json

    flat = _flatten_json(obj, separator=separator)
    lines: list[str] = []
    for key in sorted(flat):
        val = flat[key]
        if val is None:
            continue
        lines.append(f"{key}: {val}")
    return "\n".join(lines)


@dataclass
class XMLProcessor:
    """Parse XML files into flattened key-value text for RAG ingestion.

    Attributes are prefixed with ``@`` (e.g. ``@id="42"``).
    Text content is stored under ``#text``.
    Repeated sibling elements become indexed lists.
    """

    chunk_size: int = 8192
    chunk_overlap: int = 512
    text_splitter: Any | None = None
    separator: str = "."

    def process(self, xml_path: str) -> list[dict[str, Any]]:
        content = Path(xml_path).read_text(encoding="utf-8")
        source = str(xml_path)
        return self.process_xml(content, source)

    def process_xml(self, content: str, source: str = "") -> list[dict[str, Any]]:
        try:
            root = _safe_fromstring(content)
        except ET.ParseError as e:
            logger.warning("Invalid XML in %s: %s", source, e)
            return []
        except DefusedXmlException as e:
            # Rejected by defusedxml: DTD, external entity, or entity
            # expansion (XXE / billion-laughs).  Log and skip rather than
            # crashing or exhausting memory.
            logger.warning("Unsafe XML rejected in %s: %s", source, e)
            return []

        flat = _flatten_xml(root, self.separator)
        text = "\n".join(f"{k}: {v}" for k, v in sorted(flat.items()) if v is not None)

        if not self._exceeds_budget(text):
            return [{"text": text, "source": source}]

        # Chunk large XML by top-level children
        return self._chunk_large(root, source)

    # ------------------------------------------------------------------
    # Budget helpers
    # ------------------------------------------------------------------

    def _exceeds_budget(self, text: str) -> bool:
        if self.text_splitter is not None:
            return self.text_splitter.count_tokens(text) > self.chunk_size
        return len(text) > self.chunk_size

    def _exceeds_combined_buffer(self, buffer: dict[str, Any], child_text: str) -> bool:
        """Check if adding *child_text* to *buffer* exceeds the token/char budget."""
        if not buffer:
            return False
        if self.text_splitter is not None:
            combined = _flatten_to_text(buffer) + "\n" + child_text
            return self.text_splitter.count_tokens(combined) > self.chunk_size
        return sum(len(str(v)) for v in buffer.values()) + len(child_text) + 1 > self.chunk_size

    def _chunk_large(self, root: ET.Element, source: str) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        buffer: dict[str, Any] = {}
        buffer_size = 0

        # Preamble: root attributes
        root_flat = {f"@{k}": v for k, v in root.attrib.items()}
        if root_flat:
            buffer.update(root_flat)
            buffer_size = len("\n".join(f"{k}: {v}" for k, v in root_flat.items()))

        for child in root:
            child_flat = _flatten_xml(child, self.separator)
            child_text = "\n".join(f"{k}: {v}" for k, v in sorted(child_flat.items()) if v is not None)
            child_size = len(child_text)

            if self._exceeds_budget(child_text):
                if buffer:
                    docs.append({"text": _flatten_to_text(buffer), "source": source})
                    buffer = {}
                    buffer_size = 0
                from multimodal_rag.input_processing.text_processor import TextProcessor

                tp = TextProcessor(
                    chunk_size=self.chunk_size,
                    chunk_overlap=self.chunk_overlap,
                    text_splitter=self.text_splitter,
                )
                sub_chunks = tp.process_text(child_text, source)
                docs.extend(sub_chunks)
                continue

            if self._exceeds_combined_buffer(buffer, child_text):
                docs.append({"text": _flatten_to_text(buffer), "source": source})
                buffer = {}
                buffer_size = 0

            buffer[child.tag] = child_flat if len(child_flat) > 1 else next(iter(child_flat.values()), "")
            buffer_size += child_size

        if buffer:
            docs.append({"text": _flatten_to_text(buffer), "source": source})

        return docs


@dataclass
class YAMLProcessor:
    """Parse YAML files into flattened key-value text for RAG ingestion.

    Uses the same flattening strategy as :class:`JSONProcessor`.
    """

    chunk_size: int = 8192
    chunk_overlap: int = 512
    text_splitter: Any | None = None
    separator: str = "."

    def process(self, yaml_path: str) -> list[dict[str, Any]]:
        content = Path(yaml_path).read_text(encoding="utf-8")
        source = str(yaml_path)
        return self.process_yaml(content, source)

    def process_yaml(self, content: str, source: str = "") -> list[dict[str, Any]]:
        try:
            import yaml
        except ImportError:
            raise ImportError("PyYAML is required for YAML processing. Install with: pip install pyyaml")

        try:
            data = yaml.safe_load(content)
        except Exception as e:
            logger.warning("Invalid YAML in %s: %s", source, e)
            return []

        if data is None:
            return []

        from multimodal_rag.input_processing.json_processor import JSONProcessor

        jp = JSONProcessor(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            flatten=True,
            separator=self.separator,
        )
        return jp.process_data(data, source)
