"""合成固定向量的本地解码／余弦微基准，不代表线上端到端问答容量。"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.index_cache import VectorCache


def benchmark(sizes=(1000, 5000, 10000), dimension=32, repeats=5):
    results = []
    for count in sizes:
        rows = [(str(i), json.dumps([1.0 + (i + n) % 7 for n in range(dimension)])) for i in range(count)]
        cache = VectorCache();times=[];hits=[]
        for repeat in range(repeats):
            started=time.perf_counter()
            vectors, hit = cache.decode(('synthetic', str(count)), rows, dimension, 32 * 1024 * 1024)
            query = next(iter(vectors.values()))
            sorted((sum(a*b for a,b in zip(query,v)),identifier) for identifier,v in vectors.items())[-20:]
            times.append(round((time.perf_counter()-started)*1000, 3));hits.append(hit)
        results.append({'vectors':count,'dimension':dimension,'cold_ms':times[0],
                        'warm_median_ms':statistics.median(times[1:]),'cache_hits':hits,'estimated_cache_bytes':cache.size})
    return {'kind':'synthetic_decode_cosine_microbenchmark','results':results,'provider_calls':0,
            'production_capacity_verified':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=benchmark()
    with args.output.open('x',encoding='utf-8') as out:json.dump(result,out,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))
