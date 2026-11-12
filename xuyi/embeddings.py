"""Local, pinned ONNX embedding adapter; no remote execution or model code."""
import hashlib
import json
from pathlib import Path

from .store import MemoryError, digest


class ByteCounter:
    unit = "utf8_bytes"

    def count(self, text):
        return len(text.encode("utf-8"))


class TokenizerCounter:
    def __init__(self, path):
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise MemoryError("需要安装 semantic 依赖以使用本地分词器") from exc
        self.tokenizer = Tokenizer.from_file(str(path))
        self.tokenizer.no_padding()
        self.tokenizer.no_truncation()
        self.unit = "tokenizer_tokens:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]

    def count(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)


class OnnxEmbedder:
    default_prefix = "为这个句子生成表示以用于检索相关文章："

    def __init__(self, directory):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise MemoryError("语义检索需要 numpy、onnxruntime、tokenizers；未安装时可明确使用词法模式") from exc
        self.np = np
        root = Path(directory)
        manifest = json.loads((root / "source.json").read_text(encoding="utf-8"))
        for name, meta in manifest["files"].items():
            if Path(name).name != name:
                raise MemoryError("模型清单只允许同目录文件")
            if hashlib.sha256((root/name).read_bytes()).hexdigest() != meta["sha256"]:
                raise MemoryError(f"模型文件校验失败：{name}")
        # The query instruction is part of the model contract and must be
        # covered by the fingerprint, since it changes every cached vector.
        self.prefix = manifest.get("query_prefix", self.default_prefix)
        self.fingerprint = digest({"manifest": manifest, "pooling": "CLS-L2", "prefix": self.prefix, "max_tokens": 512})
        self.tokenizer = Tokenizer.from_file(str(root/"tokenizer.json"))
        self.tokenizer.no_truncation()
        options = ort.SessionOptions(); options.intra_op_num_threads = 2; options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(root/"model.onnx"), sess_options=options, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def fit_prefix(self, text, limit):
        self.tokenizer.no_padding()
        end = min(len(text), limit)
        while end and len(self.tokenizer.encode(text[:end]).ids) > 512:
            end //= 2
        if not end:
            raise MemoryError("文本无法装入嵌入模型窗口")
        return end

    def encode(self, texts, query=False):
        self.tokenizer.no_padding()
        strings = [self.prefix+t if query else t for t in texts]
        if any(len(self.tokenizer.encode(t).ids) > 512 for t in strings):
            raise MemoryError("检索文本超过嵌入窗口；请缩短问题，不能静默截断")
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        vectors = []
        for start in range(0, len(strings), 16):
            batch = self.tokenizer.encode_batch(strings[start:start+16])
            feed = {name: self.np.array([getattr(x, attr) for x in batch], dtype=self.np.int64)
                    for name, attr in (("input_ids","ids"),("attention_mask","attention_mask"),("token_type_ids","type_ids")) if name in self.inputs}
            out = self.session.run(None, feed)[0][:, 0, :]
            norms = self.np.linalg.norm(out, axis=1, keepdims=True)
            if not self.np.isfinite(out).all() or self.np.any(norms == 0):
                raise MemoryError("嵌入返回无效向量")
            vectors.extend((out/norms).tolist())
        return vectors
