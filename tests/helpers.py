import tempfile
from pathlib import Path
from scanhub.storage import inspect_file


def seed(client, contents, device="test-scanner", qr="qr-123", batch=""):
    """Seed persisted originals for management tests; network ingress has separate FTP tests."""
    store=client.app.state.store
    with tempfile.TemporaryDirectory(dir=store.root/"tmp") as directory:
        prepared=[]
        for index,(name,data) in enumerate(contents):
            path=Path(directory)/str(index)
            path.write_bytes(data)
            mime,width,height=inspect_file(path)
            prepared.append(dict(temp=path,name=name,mime=mime,width=width,height=height))
        return [r["id"] for r in store.ingest(prepared,device,qr,'{}',external_batch=batch or None,workspace_id=client.workspace_id)]
