import chromadb
c = chromadb.HttpClient(host='localhost', port=8000)
inc = c.get_collection('incidents')
print('Total incidents:', inc.count())
res = inc.peek(limit=5)
print('Keys in metadata:', list(res['metadatas'][0].keys()) if res['metadatas'] else 'none')
for i, m in enumerate(res['metadatas'][:3]):
    print('Incident %d:' % i, {k: m[k] for k in ['alertname','alert_name','namespace','outcome'] if k in m})
# Search for HighCPU by ID prefix
all_ids = inc.get(include=[])['ids']
cpu_ids = [i for i in all_ids if 'HighCPU' in i or 'CPU' in i or 'cpu' in i]
print('IDs with CPU/HighCPU:', cpu_ids[:10])
