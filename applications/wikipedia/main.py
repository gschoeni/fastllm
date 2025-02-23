from itertools import product
import json
import asyncio
import subprocess
from pathlib import Path
import time
import os

from modal import Image, Stub, Volume, gpu, method, Secret

N_GPU = 10
GPU_CONFIG = gpu.A10G()
MODEL_ID = "BAAI/bge-small-en-v1.5"
MODEL_SLUG = MODEL_ID.split("/")[-1]

BATCH_SIZE = 512
DOCKER_IMAGE = (
    "ghcr.io/huggingface/text-embeddings-inference:86-0.4.0"  # Ampere 86 for A10s.
    # "ghcr.io/huggingface/text-embeddings-inference:0.4.0" # Ampere 80 for A100s.
    # "ghcr.io/huggingface/text-embeddings-inference:0.3.0"  # Turing for T4s.
)
dataset_name = "wikipedia"
volume = Volume.persisted("embedding-wikipedia")
cache_dir = "/data"
data_dir = f"{cache_dir}/{dataset_name}"
DATA_PATH = Path(data_dir)
# set OXEN_CONFIG_DIR environment var to data_dir
os.environ["OXEN_CONFIG_DIR"] = data_dir


PUSH_TO_HUB = True
# dataset_name = f"567-labs/wikipedia-embedding-{MODEL_SLUG}-sample"
dataset_name = "oxbot/Wikipedia-Embeddings"
dataset_file = "wiki-embeddings.parquet"

LAUNCH_FLAGS = [
    "--model-id",
    MODEL_ID,
    "--port",
    "8000",
    "--max-client-batch-size",
    str(BATCH_SIZE),
    "--max-batch-tokens",
    str(BATCH_SIZE * 512),
]


def spawn_server() -> subprocess.Popen:
    import socket

    process = subprocess.Popen(["text-embeddings-router"] + LAUNCH_FLAGS)

    # Poll until webserver at 127.0.0.1:8000 accepts connections before running inputs.
    while True:
        try:
            socket.create_connection(("127.0.0.1", 8000), timeout=1).close()
            print("Webserver ready!")
            return process
        except (socket.timeout, ConnectionRefusedError):
            # Check if launcher webserving process has exited.
            # If so, a connection can never be made.
            retcode = process.poll()
            if retcode is not None:
                raise RuntimeError(f"launcher exited unexpectedly with code {retcode}")


def download_model():
    # Wait for server to start. This downloads the model weights when not present.
    spawn_server()


stub = Stub("embeddings")


tei_image = (
    Image.from_registry(
        "ghcr.io/huggingface/text-embeddings-inference:86-0.4.0",
        add_python="3.10",
    )
    .dockerfile_commands("ENTRYPOINT []")
    .run_function(download_model, gpu=GPU_CONFIG)
    .pip_install("httpx")
)


with tei_image.imports():
    import numpy as np


def generate_chunks_from_dataset(xs, chunk_size: int):
    for data in xs:
        id_ = data["id"]
        url = data["url"]
        title = data["title"]
        text = data["text"]
        for chunk_start in range(0, len(text), chunk_size):
            yield (
                id_,
                url,
                title,
                text[chunk_start : chunk_start + chunk_size],
            )


def generate_batches(xs, batch_size):
    batch = []
    for x in xs:
        batch.append(x)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


@stub.cls(
    gpu=GPU_CONFIG,
    image=tei_image,
    # Use up to 10 GPU containers at once.
    concurrency_limit=N_GPU,
    retries=3,
)
class TextEmbeddingsInference:
    def __enter__(self):
        # If the process is running for a long time, the client does not seem to close the connections, results in a pool timeout
        from httpx import AsyncClient

        self.process = spawn_server()
        self.client = AsyncClient(base_url="http://127.0.0.1:8000", timeout=30)

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.process.terminate()

    async def _embed(self, chunk_batch):
        texts = [chunk[3] for chunk in chunk_batch]
        res = await self.client.post("/embed", json={"inputs": texts})
        return np.array(res.json())

    @method()
    async def embed(self, chunks):
        """Embeds a list of texts.  id, url, title, text = chunks[0]"""

        coros = [
            self._embed(chunk_batch)
            for chunk_batch in generate_batches(chunks, batch_size=BATCH_SIZE)
        ]

        embeddings = np.concatenate(await asyncio.gather(*coros))
        return chunks, embeddings


@stub.function(
    image=Image.debian_slim()
        .pip_install("oxenai", "pyarrow", "tqdm", "requests"),
    volumes={cache_dir: volume},
    timeout=84600,
    secret=Secret.from_name("oxenai-api-key"),
)
def embed_dataset(
    # down_scale: float = 0.005,
    sample_size: int = 5000,
    batch_size: int = 512 * 50
):
    from oxen.streaming_dataset import load_dataset
    from oxen.auth import config_auth as config_oxen_auth
    from oxen.remote_repo import create_repo

    import oxen
    import pyarrow as pa
    import pyarrow.parquet as pq
    import time
    import datetime
    import os
    from tqdm import tqdm

    start = time.perf_counter()
    # Load the dataset from https://www.oxen.ai/datasets/Wikipedia
    print("Streaming dataset from Oxen.ai...")
    # dataset = load_dataset("datasets/Wikipedia", directory="data")
    dataset = load_dataset("datasets/Wikipedia", paths="data/xaa.arrow")
    print(f"Dataset loaded in {time.perf_counter()-start:.2f} seconds")

    # Extract the total size of the dataset
    ttl_size = len(dataset)

    # Counting all characters in the dataset
    dataset_chars = 19560538957  # sum(map(len, dataset["train"]["text"]))
    print(f"Total dataset characters: {dataset_chars}")

    # sample_size = int(ttl_size * down_scale)
    print(f"Calculated dataset size of {ttl_size} and sample size of {sample_size}")

    # Iterate over the first 5% of the dataset's rows
    subset = []
    for i in tqdm(range(sample_size)):
        subset.append(dataset[i])
    model = TextEmbeddingsInference()

    print(f"Working with {sample_size} rows")

    text_chunks = generate_chunks_from_dataset(subset, chunk_size=512)
    batches = generate_batches(text_chunks, batch_size=batch_size)

    start = time.perf_counter()
    acc_chunks = []
    embeddings = []
    for batch_chunks, batch_embeddings in model.embed.map(batches, order_outputs=False):
        acc_chunks.extend(batch_chunks)
        embeddings.extend(batch_embeddings)

    end = time.perf_counter()

    duration = end - start
    characters = sum(map(len, [chunk[3] for chunk in acc_chunks]))
    characters_per_sec = int(characters / duration)
    extrapolated_duration_cps_fmt = str(
        datetime.timedelta(seconds=dataset_chars / characters_per_sec)
    )
    resp = {
        # "downscale": down_scale,
        "sample_size": sample_size,
        "batch_size": batch_size,
        "n_gpu": N_GPU,
        "duration_mins": duration / 60,
        "characters_per_sec": characters_per_sec,
        "extrapolated_duration": extrapolated_duration_cps_fmt,
    }

    if PUSH_TO_HUB:
        print(f"Pushing to hub {dataset_name}")
        oxenai_token = os.environ["OXENAI_API_KEY"]
        config_oxen_auth(oxenai_token)

        # Initialize the Oxen Repository
        repo = oxen.init(data_dir)
        
        table = pa.Table.from_arrays(
            [
                pa.array([chunk[0] for chunk in acc_chunks]),  # id
                pa.array([chunk[1] for chunk in acc_chunks]),  # url
                pa.array([chunk[2] for chunk in acc_chunks]),  # title
                pa.array([chunk[3] for chunk in acc_chunks]),  # text
                pa.array(embeddings),
            ],
            names=["id", "url", "title", "text", "embedding"],
        )
        pq.write_table(table, os.path.join(data_dir, dataset_file))
        repo.add(dataset_file)
        print(repo.status())
        repo.commit("Adding embeddings")

        remote_repo = create_repo(dataset_name)
        print("Created remote repo")
        print(remote_repo)
        print(remote_repo.url())

        repo.set_remote("origin", remote_repo.url())
        repo.push()

    return resp


@stub.local_entrypoint()
def main():
    # for scale, batch_size in product([0.25], [512 * 50]):
    #     with open("benchmarks.json", "a") as f:
    #         benchmark = embed_dataset.remote(down_scale=scale, batch_size=batch_size)
    #         print(json.dumps(benchmark, indent=2))
    #         f.write(json.dumps(benchmark, indent=2) + "\n")
    
    benchmark = embed_dataset.remote(sample_size=50, batch_size=10)
    print(json.dumps(benchmark, indent=2))
