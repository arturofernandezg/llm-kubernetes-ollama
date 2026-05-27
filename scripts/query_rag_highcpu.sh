#!/usr/bin/env bash
# Consulta ChromaDB para ver qué contexto RAG devuelve para HighCPU
# Uso: bash scripts/query_rag_highcpu.sh
set -e

PORT=8002
NS=arturo-llm-test

echo "==> Port-forward ChromaDB..."
kubectl port-forward svc/chromadb-svc $PORT:8000 -n $NS &>/dev/null &
PF_PID=$!
sleep 3

echo "==> Listando colecciones..."
COLS=$(curl -s "localhost:$PORT/api/v1/collections")
echo "$COLS" | python3 -c "import sys,json;[print(f'  {c[\"name\"]} id={c[\"id\"]}') for c in json.load(sys.stdin)]"

# Obtener IDs de colecciones
RUNBOOKS_ID=$(echo "$COLS" | python3 -c "import sys,json;cols=json.load(sys.stdin);print(next((c['id'] for c in cols if 'runbook' in c['name'].lower()),''))")
INCIDENTS_ID=$(echo "$COLS" | python3 -c "import sys,json;cols=json.load(sys.stdin);print(next((c['id'] for c in cols if 'incident' in c['name'].lower()),''))")

echo ""
echo "==> Runbooks top-5 para 'HighCPU container CPU usage sustained above threshold':"
curl -s -X POST "localhost:$PORT/api/v1/collections/$RUNBOOKS_ID/query" \
  -H 'Content-Type: application/json' \
  -d '{"query_texts":["HighCPU container CPU usage sustained above threshold kubernetes pod"],"n_results":5,"include":["documents","metadatas","distances"]}' \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
docs=d.get('documents',[[]])[0]
metas=d.get('metadatas',[[]])[0]
dists=d.get('distances',[[]])[0]
for i,(doc,meta,dist) in enumerate(zip(docs,metas,dists),1):
    print(f'  [{i}] dist={dist:.3f} id={meta.get(\"id\",\"?\")} class={meta.get(\"error_class\",\"?\")}')
    print(f'      {doc[:120]}...')
    print()
"

echo "==> Incidents top-5 para 'HighCPU container CPU usage':"
curl -s -X POST "localhost:$PORT/api/v1/collections/$INCIDENTS_ID/query" \
  -H 'Content-Type: application/json' \
  -d '{"query_texts":["HighCPU container CPU usage kubernetes pod"],"n_results":5,"include":["documents","metadatas","distances"]}' \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
docs=d.get('documents',[[]])[0]
metas=d.get('metadatas',[[]])[0]
dists=d.get('distances',[[]])[0]
for i,(doc,meta,dist) in enumerate(zip(docs,metas,dists),1):
    print(f'  [{i}] dist={dist:.3f} alertname={meta.get(\"alertname\",\"?\")} outcome={meta.get(\"outcome\",\"?\")}')
    print(f'      {doc[:120]}...')
    print()
"

kill $PF_PID 2>/dev/null
echo "==> Done."
