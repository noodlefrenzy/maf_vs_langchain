"""Harden the pinned MAF file checkpoint saver for local compatibility runs."""

import os

from agent_framework import FileCheckpointStorage, WorkflowCheckpoint


class OwnerOnlyCheckpointStorage(FileCheckpointStorage):
    """Make MAF's atomic temporary checkpoint owner-only before serialization."""

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        destination = self._validate_file_path(checkpoint.checkpoint_id)
        temporary = destination.with_suffix(".json.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            checkpoint_id = await super().save(checkpoint)
        finally:
            if temporary.exists():
                temporary.unlink()

        destination.chmod(0o600)
        return checkpoint_id
