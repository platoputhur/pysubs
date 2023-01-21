import hashlib
import json
import logging
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import UploadFile
from google.api_core.exceptions import PermissionDenied
from pysubs.dal.datastore_models import MediaModel, SubtitleModel, UserModel
from pysubs.dal.firestore import FirestoreDatastore
from pysubs.exceptions.media import NotEnoughCreditsToPerformGenerationError
from pysubs.interfaces.asr import ASR
from pysubs.interfaces.media import MediaManager
from pysubs.utils.models import Media, MediaSource, MediaType, Transcription, Subtitle
from pysubs.utils.transcriber import WhisperTranscriber
from pysubs.utils.media.youtube import YouTubeMediaManager
from pysubs.utils.media.file import FileMediaManager
from pysubs.utils.constants import LogConstants

logger = logging.getLogger(LogConstants.LOGGER_NAME)

SECONDS_PER_ONE_CREDIT: int = 300


def get_yt_media_info(video_url: str) -> Media:
    """
    helper function to get the media info for YouTube video url
    :param video_url:
    :return:
    """
    mgr: MediaManager = YouTubeMediaManager()
    return mgr.get_media_info(video_url=video_url)


def check_if_user_can_generate(media: Media, user: UserModel) -> bool:
    """
    Check if the user has enough credit to do the subtitle generation
    TODO: This can be added to the api endpoint using depends
    :param media:
    :param user:
    :return:
    """
    duration = media.duration
    available_credits = user.credits
    required_credits = (duration.seconds // SECONDS_PER_ONE_CREDIT) or 1
    if available_credits - required_credits >= 0:
        return True
    else:
        return False


def process_yt_video_url_and_generate_subtitles(video_url: str, user: UserModel):
    """
    helper function to process the video from YouTube url and generate the subtitles
    :param video_url:
    :param user:
    :return:
    """
    audio = get_audio_from_yt_video(video_url=video_url, user=user)
    logger.info(f"Audio generated for the video url: {video_url}")
    transcription = get_subtitles_from_audio(audio=audio)
    logger.info(f"Audio transcription finished for the video url: {video_url}")
    save_transcription_attempt(audio, transcription, user)
    logger.info(f"Saved data to datastore.")


def process_uploaded_file_and_generate_subtitles(file: UploadFile, user: UserModel):
    """
    helper function to process the uploaded video file and generate the subtitles
    :param file:
    :param user:
    :return:
    """
    audio = get_audio_from_video_file(file=file, user=user)
    logger.info(f"Audio generated for the uploaded video file: {file.filename}")
    transcription = get_subtitles_from_audio(audio=audio)
    logger.info(f"Audio transcription finished for the video file: {file.filename}")
    save_transcription_attempt(audio, transcription, user)
    logger.info(f"Saved data to datastore.")


def get_audio_from_yt_video(video_url: str, user: UserModel) -> Media:
    """
    helper function to get the audio file from the YouTube video url
    :param video_url:
    :param user:
    :return:
    """
    mgr: MediaManager = YouTubeMediaManager()
    media: Media = Media(
        id=generate_media_id(media_url=video_url, user=user),
        title=None,
        content=None,
        duration=None,
        source=MediaSource.YOUTUBE,
        file_type=MediaType.MP4,
        local_storage_path=None,
        source_url=video_url,
        thumbnail_url=None
    )
    video = mgr.download(media=media)
    audio = mgr.convert(media=video, to_type=MediaType.MP3)
    return audio


def get_audio_from_video_file(file: UploadFile, user: UserModel) -> Media:
    """
    helper function to get the audio file from the uploaded video file
    :param file:
    :param user:
    :return:
    """
    mgr: MediaManager = FileMediaManager()
    media = mgr.get_media_info(video_file=file)
    media: Media = Media(
        id=generate_media_id(media_url=media.local_storage_path, user=user),
        title=media.title,
        content=None,
        duration=media.duration,
        source=media.source,
        file_type=MediaType.MP4,
        local_storage_path=media.local_storage_path,
        source_url=media.source_url,
        thumbnail_url=media.thumbnail_url
    )
    video = mgr.download(media=media)
    audio = mgr.convert(media=video, to_type=MediaType.MP3)
    return audio


def generate_transcription_id(media_id: str, language: str) -> str:
    """
    Helper function to generate the transcription id
    This helps to get data from the data store without searching.
    :param media_id:
    :param language:
    :return:
    """
    key_helper_dict = OrderedDict({
        "media_id": media_id,
        "language": language
    })
    key_helper = json.dumps(key_helper_dict).encode("utf-8")
    return hashlib.sha256(key_helper).hexdigest()


def generate_media_id(media_url: str, user: UserModel) -> str:
    """
    helper function to generate the media id
    This helps to get data from the data store without searching.
    :param media_url:
    :param user:
    :return:
    """
    key_helper_dict = OrderedDict({
        "media_url": media_url,
        "user_id": user.id
    })
    key_helper = json.dumps(key_helper_dict).encode("utf-8")
    return hashlib.sha256(key_helper).hexdigest()


def get_subtitles_from_audio(audio: Media) -> Transcription:
    """
    helper function to generate the transcription
    :param audio:
    :return:
    """
    transcriber: ASR = WhisperTranscriber()
    result = transcriber.process_audio(audio=audio)
    language = transcriber.get_detected_language(processed_data=result)
    content = transcriber.generate_subtitles(processed_data=result)
    return Transcription(
        id=generate_transcription_id(media_id=audio.id, language=language),
        content=content,
        language=language,
        media_id=audio.id
    )


def start_youtube_transcribe_worker(video_url: str, user: UserModel) -> None:
    """
    starts the worker in a separate thread
    :param video_url:
    :param user:
    :return:
    """
    thr = threading.Thread(target=process_yt_video_url_and_generate_subtitles, args=(video_url, user,))
    thr.start()


def start_video_file_transcribe_worker(file: UploadFile, user: UserModel) -> None:
    """
    starts the worker in a separate thread
    :param file:
    :param user:
    :return:
    """
    thr = threading.Thread(target=process_uploaded_file_and_generate_subtitles, args=(file, user,))
    thr.start()


def save_transcription_attempt(audio: Media, transcription: Transcription, user: UserModel) -> None:
    """
    Saves the transcription attempt in the datastore
    :param audio:
    :param transcription:
    :param user:
    :return:
    """
    fs = FirestoreDatastore.instance()
    ds_user = fs.get_user(user.id)
    ds_user.credits = get_remaining_credits(media=audio, user=user)
    current_time = datetime.utcnow()
    ds_media = MediaModel(
        id=audio.id,
        user_id=user.id,
        title=audio.title,
        duration=audio.duration.seconds,
        media_url=audio.source_url,
        media_source=audio.source.value,
        thumbnail_url=audio.thumbnail_url,
        created_at=current_time
    )
    expire_at = current_time + timedelta(days=10)
    ds_subtitle = SubtitleModel(
        id=transcription.id,
        media_id=transcription.media_id,
        content=transcription.content,
        created_at=current_time,
        expire_at=expire_at
    )

    try:
        fs.upsert_media(ds_media)
        fs.upsert_subtitle(ds_subtitle)
        fs.upsert_user(ds_user)
    except PermissionDenied as e:
        logger.error(f"Error due to insufficient permissions for adding data to Firestore, error: {e}")


def get_subtitle_generation_status(video_url: str, user: UserModel) -> tuple[Optional[MediaModel], Optional[SubtitleModel]]:
    """
    helper function to get the subtitle generation status from the datastore.
    :param video_url:
    :param user:
    :return:
    """
    media_id = generate_media_id(video_url, user)
    fs = FirestoreDatastore.instance()
    if media := fs.get_media(media_id=media_id):
        if subtitle := fs.get_subtitle_for_media(media_id=media_id):
            return media, subtitle
    else:
        return None, None


def get_history(last_created_at: Optional[datetime], count: int, user: UserModel) -> list[Subtitle]:
    """
    helper function to get the previous subtitle generation entries
    :param last_created_at:
    :param count:
    :param user:
    :return:
    """
    fs = FirestoreDatastore.instance()
    history = fs.get_history_for_user(user_id=user.id, last_created_at=last_created_at, count=count)
    resp: list[Subtitle] = []
    for item in history:
        media = item.media
        subtitles = item.subtitles
        for sub in subtitles:
            resp.append(
                Subtitle(
                    subtitle_id=sub.id,
                    video_url=media.media_url,
                    title=media.title,
                    video_length=media.duration,
                    thumbnail=media.thumbnail_url,
                    subtitle=sub.content,
                    created_at=media.created_at
                )
            )
    return resp


def get_remaining_credits(media: Media, user: UserModel) -> int:
    """
    helper function to get the remaining credits a user has
    :param media:
    :param user:
    :return:
    """
    duration = media.duration
    available_credits = user.credits
    required_credits = (duration.seconds // SECONDS_PER_ONE_CREDIT) or 1
    if available_credits - required_credits < 0:
        raise NotEnoughCreditsToPerformGenerationError(
            f"Not enough credits available to generate subtitles for the media: {media.title}"
        )
    else:
        return available_credits - required_credits
