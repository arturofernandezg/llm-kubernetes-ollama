import chromadb
c = chromadb.HttpClient(host='localhost', port=8000)
rb = c.get_collection('runbooks')
res = rb.get(where={'error_class': 'HighCPU'}, include=['documents','metadatas'])
print('HighCPU runbooks encontrados:', len(res['ids']))
for doc, meta in zip(res['documents'], res['metadatas']):
    print('  id=%s class=%s' % (meta.get('id'), meta.get('error_class')))
    print('  doc preview: %s' % doc[:200])
inc = c.get_collection('incidents')
res2 = inc.get(where={'alertname': 'HighCPU'}, include=['documents','metadatas'])
print('HighCPU incidents: %d' % len(res2['ids']))
for m in res2['metadatas'][:5]:
    print('  outcome=%s ns=%s' % (m.get('outcome'), m.get('namespace')))
