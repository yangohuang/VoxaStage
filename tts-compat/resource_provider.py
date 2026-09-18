"""Resolve TTS audio resources without coupling callers to the database."""

import os
import json


def is_fixed_resource_mode(settings):
    """Return whether the service should use one configured local speaker."""
    return settings.RESOURCE_MODE.strip().lower() == "fixed"


def uses_database_resources(settings):
    """Return whether database-backed resources should be initialized."""
    return not is_fixed_resource_mode(settings)


def _fixed_audio(settings):
    speaker_id = settings.FIXED_SPEAKER_ID.strip()
    wav_path = settings.FIXED_SPEAKER_WAV_PATH.strip()
    if not speaker_id:
        raise ValueError("FIXED_SPEAKER_ID is required in fixed mode")
    if not wav_path:
        raise ValueError("FIXED_SPEAKER_WAV_PATH is required in fixed mode")
    return {"id": speaker_id, "wav_local_path": wav_path}


def _local_audios(settings):
    """Optional server-owned voice map, also loaded by the existing startup path."""
    audios = [_fixed_audio(settings)]
    path = os.getenv("LOCAL_SPEAKERS_JSON", "")
    if not path:
        return audios
    with open(path, encoding="utf-8") as stream:
        extras = json.load(stream)
    if not isinstance(extras, list) or len(extras) > 7:
        raise ValueError("LOCAL_SPEAKERS_JSON must contain at most 7 extra voices")
    ids = {audios[0]["id"]}
    for item in extras:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"] or item["id"] in ids
                or not isinstance(item.get("wav_local_path"), str)
                or not os.path.isabs(item["wav_local_path"])):
            raise ValueError("Invalid or duplicate local voice mapping")
        ids.add(item["id"])
        audios.append({"id": item["id"], "wav_local_path": item["wav_local_path"]})
    return audios


def get_audio_by_id(audio_id, settings, database_loader=None):
    """Resolve an audio request from fixed configuration or the database."""
    if is_fixed_resource_mode(settings):
        audios = _local_audios(settings)
        return next((item for item in audios if item["id"] == str(audio_id)), audios[0])
    if database_loader is None:
        from db.crud import get_audio_by_id as database_loader
    return database_loader(audio_id)


def get_available_audios(settings, database_loader=None):
    """List fixed or database-backed audio resources."""
    if is_fixed_resource_mode(settings):
        return _local_audios(settings)
    if database_loader is None:
        from db.crud import get_avaliable_audios as database_loader
    return database_loader()


def resolve_speaker(audio_id, speed, settings, database_loader=None):
    """Resolve the actual speaker key and WAV path for one request."""
    audio = get_audio_by_id(audio_id, settings, database_loader)
    if audio is None:
        return None, None
    speaker_id = audio["id"] if speed == 1.0 else f"{audio['id']}_{speed}"
    wav_path = audio["wav_local_path"]
    if speed != 1.0:
        stem, suffix = os.path.splitext(wav_path)
        wav_path = f"{stem}_{speed}{suffix}"
    return speaker_id, wav_path
