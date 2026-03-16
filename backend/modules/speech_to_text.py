import os
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

class STTEngine:
    def __init__(self):
        self.api_key = os.environ.get("DEEPGRAM_API_KEY")
        if not self.api_key:
            print("WARNING: DEEPGRAM_API_KEY is missing in .env")
        else:
            self.client = DeepgramClient(self.api_key)

    def create_live_stream(self, language: str, on_transcript_callback, on_close_callback=None):
        """
        Creates and returns a Deepgram Live Client connection.
        on_close_callback() is called when Deepgram closes (cleanly or abnormally).
        """
        dg_language = "hi" if language == "hi" else "en"

        dg_connection = self.client.listen.live.v("1")

        def on_message(self_inner, result, **kwargs):
            transcript = ""
            if result.channel and result.channel.alternatives:
                transcript = result.channel.alternatives[0].transcript

            is_final = getattr(result, "is_final", False)
            speech_final = getattr(result, "speech_final", False)

            if transcript or speech_final:
                on_transcript_callback(transcript, is_final, speech_final)

        def on_error(self_inner, error, **kwargs):
            print(f"Deepgram Error: {error}")

        def on_close(self_inner, close, **kwargs):
            print(f"_signal_exit  - ConnectionClosed: {close}")
            if on_close_callback:
                on_close_callback()

        def on_utterance_end(self_inner, utterance_end, **kwargs):
            """
            Fires when Deepgram is confident the user has completely finished speaking.
            Secondary signal to endpointing — more reliable in noisy environments.
            Trigger as speech_final so the backend fires the LLM pipeline immediately.
            """
            on_transcript_callback("", is_final=False, speech_final=True)

        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
        dg_connection.on(LiveTranscriptionEvents.Error, on_error)
        dg_connection.on(LiveTranscriptionEvents.Close, on_close)

        options = LiveOptions(
            model="nova-2",
            language=dg_language,
            endpointing=200,          # ms silence → speech_final (primary signal)
            utterance_end_ms="1000",  # ms → UtteranceEnd (secondary safety net)
            interim_results=True
        )

        if dg_connection.start(options) is False:
            print("Failed to connect to Deepgram")
            return None

        return dg_connection

stt_engine = STTEngine()
