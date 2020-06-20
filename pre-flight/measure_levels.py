# Check that audio can be played... at all.
import pyogg
import time
import sounddevice as sd
import numpy
from enum import Enum
import time
import threading
import math
import operator
import wave
from tone import Tone
from fft_analyser import FFTAnalyser
        
# Measure Levels
# ==============

# Measure levels of silence at tone #0's frequency.  Then play tone #0
# and ask user to increase the system volume until non-silence is
# detected.  From here, ask the user not to change the system volume.
#
# Then, measure levels of tone #0 and not-tone #1. Play tone #0 and
# tone #1 and wait until we detect not-not-tone#1.  Finally, measure
# levels of tone #0 and tone #1.

def measure_levels(desired_latency="low", samples_per_second=48000):
    # Get number of channels from input and output devices
    device_strings = sd.query_devices()
    default_input_index = sd.default.device[0]
    default_output_index = sd.default.device[1]

    input_device_string = device_strings[default_input_index]["name"]
    print("Selected input device:", input_device_string)
    input_channels = device_strings[default_input_index]["max_input_channels"]
    print("Number of input channels:", input_channels)

    output_device_string = device_strings[default_output_index]["name"]
    print("Selected output device:", output_device_string)
    output_channels = device_strings[default_output_index]["max_output_channels"]
    print("Number of output channels:", output_channels)

    channels = (input_channels, output_channels)

    
    # Class to describe the current state of the process
    class ProcessState(Enum):
        RESET = 0
        MEASURE_SILENCE = 10
        FADEIN_TONE0 = 20
        DETECT_TONE0 = 30
        MEASURE_TONE0 = 40
        DETECT_TONE1 = 50
        MEASURE_TONE0_TONE1 = 60
        FADEOUT_TONE0_TONE1 = 61
        DETECT_SILENCE = 63
        MEASURE_SILENCE2 = 67
        COMPLETING = 70
        COMPLETED = 80
        ABORTED = 90


    # Create a class to hold the shared variables
    class SharedVariables:
        def __init__(self, samples_per_second):
            # DEBUG
            # Create a buffer to store the outdata for analysis
            duration = 20 # seconds
            self.out_pcm = numpy.zeros((
                duration*samples_per_second,
                output_channels
            ))
            self.out_pcm_pos = 0

            
            # Threading event on which the stream can wait
            self.event = threading.Event()
            
            self.samples_per_second = samples_per_second
        
            # Allocate space for recording
            max_recording_duration = 2 #seconds
            max_recording_samples = max_recording_duration * samples_per_second
            self.rec_pcm = numpy.zeros((
                max_recording_samples,
                input_channels
            ))

            # Initialise recording position
            self.rec_position = 0

            # Current state of the process
            self.process_state = ProcessState.RESET

            # Instance of the Fast Fourier Transform (FFT) analyser
            self.fft_analyser = FFTAnalyser(samples_per_second)

            # Variable to record when we entered the current state
            self.state_start_time = None

            # Specifiy the minimum number of samples
            self.min_num_samples = 50

            # Variables for tones
            tone_duration = 1 # second
            n_tones = len(self.fft_analyser.freqs)
            self.tones = [Tone(freq,
                               self.samples_per_second,
                               output_channels,
                               max_level = 1/n_tones,
                               duration = tone_duration)
                          for freq in self.fft_analyser.freqs]

            
            # Variables for the measurement of levels of silence
            self.silence_threshold_duration = 0.5 # seconds
            self.silence_levels = []
            self.silence_start_time = None
            self.silence_mean = None
            self.silence_sd = None
            self.silence_mean_threshold = 1e-6
            self.silence_sd_threshold = 1e-6
            self.silence_max_time_in_state = 5 # seconds

            # Variables for fadein tone0
            self.fadein_tone0_duration = 50/1000 # seconds
            
            # Variables for non-silence
            self.non_silence_threshold_num_sd = 4 # number of std. deviations away from silence
            self.non_silence_threshold_duration = 0.5 # seconds of non-silence
            self.non_silence_start_time = None
            self.non_silence_detected = False
            self.non_silence_abort_start_time = None
            self.non_silence_max_time_in_state = 5 # seconds

            # Variables for measurement of tone0 and not-tone1
            self.tone0_levels = []
            self.tone0_start_time = None
            self.tone0_threshold_duration = 1.5 # seconds of tone0 and not tone1
            self.tone0_mean = None
            self.tone0_sd = None

            # Variables for detection of tone1
            self.detect_tone1_threshold_num_sd = 4 # number of std. deviations away from not-tone1
            self.detect_tone1_threshold_duration = 0.5 # seconds of not-not-tone1
            self.detect_tone1_start_time = None
            self.detect_tone1_detected = False
            self.detect_tone1_max_time_in_state = 5 # seconds

            # Variables for measurement of tone0 and tone1
            self.tone0_tone1_levels = []
            self.tone0_tone1_start_time = None
            self.tone0_tone1_threshold_duration = 1.5 # seconds of tone0 and tone1
            self.tone0_tone1_mean = None
            self.tone0_tone1_sd = None

            # Variables for detect silence
            self.detect_silence_detected = False
            self.detect_silence_threshold_num_sd = 4 # std. deviations from tone0_tone1
            self.detect_silence_start_time = None
            self.detect_silence_threshold_duration = 0.2 # seconds
            self.detect_silence_max_time_in_state = 5 # seconds

            # Variables for measure silence 2
            self.measure_silence2_max_recording_duration = 0.5 #seconds
            max_recording_samples = int(
                self.measure_silence2_max_recording_duration
                * samples_per_second
            )
            self.measure_silence2_pcm = numpy.zeros((
                max_recording_samples,
                input_channels
            ))
            self.measure_silence2_pcm_pos = 0
            self.measure_silence2_start_time = None
            self.silence2_mean = None
            self.silence2_sd = None
            
            # Variable to store error during audio processing
            self.error = None
            self.exception = None

            
    # Create an instance of the shared variables
    v = SharedVariables(samples_per_second)
    
    # Callback for when the recording buffer is ready.  The size of the
    # buffer depends on the latency requested.
    def callback(indata, outdata, samples, time, status):
        nonlocal v

        try:
            # Store Recording
            # ===============

            v.rec_pcm[v.rec_position:v.rec_position+samples] = indata[:]
            v.rec_position += samples


            # Analysis
            # ========

            tones_level = v.fft_analyser.run(v.rec_pcm, v.rec_position)
            #print(tones_level)


            # Clear the first half second of the recording buffer if we've
            # recorded more than one second
            if v.rec_position > v.samples_per_second:
                seconds_to_clear = 0.5 # seconds
                samples_to_clear = int(seconds_to_clear * v.samples_per_second)
                v.rec_pcm = numpy.roll(v.rec_pcm, -samples_to_clear, axis=0)
                v.rec_position -= samples_to_clear


            # Transitions
            # ===========

            previous_state = v.process_state

            if v.process_state == ProcessState.RESET:
                v.process_state = ProcessState.MEASURE_SILENCE

            elif v.process_state == ProcessState.MEASURE_SILENCE:
                if v.silence_mean is not None and \
                   v.silence_sd is not None:
                    if any(v.silence_mean < v.silence_mean_threshold):
                        v.error = "Implausibly low mean level detected for silence; aborting."
                        v.process_state = ProcessState.ABORTED
                    elif any(v.silence_sd < v.silence_sd_threshold):
                        v.error = "Implausibly low standard deviation detected for silence; aborting."
                        v.process_state = ProcessState.ABORTED
                    else:
                        # No reason not to continue
                        v.process_state = ProcessState.FADEIN_TONE0
                        v.non_silence_abort_start_time = time.currentTime
                        print("About to play tone.  Please increase system volume until the tone is detected.")

                if time.currentTime - v.state_start_time > v.silence_max_time_in_state:
                    v.error = "Spent too long listening to silence."
                    v.process_state = ProcessState.ABORTED


            elif v.process_state == ProcessState.FADEIN_TONE0:
                v.process_state = ProcessState.DETECT_TONE0


            elif v.process_state == ProcessState.DETECT_TONE0:
                if v.non_silence_detected:
                    v.process_state = ProcessState.MEASURE_TONE0
                    print("Base tone detected.  Please do not adjust system volume nor position")
                    print("of microphone or speakers")

                if time.currentTime - v.state_start_time > v.non_silence_max_time_in_state:
                    v.error = "Spent too long listening for non-silence."
                    v.process_state = ProcessState.ABORTED


            elif v.process_state == ProcessState.MEASURE_TONE0:
                if v.tone0_mean is not None and \
                   v.tone0_sd is not None:
                    v.process_state = ProcessState.DETECT_TONE1


            elif v.process_state == ProcessState.DETECT_TONE1:
                if v.detect_tone1_detected:
                    v.process_state = ProcessState.MEASURE_TONE0_TONE1

                if time.currentTime - v.state_start_time > v.detect_tone1_max_time_in_state:
                    v.error = "Spent too long listening for tone #1."
                    v.process_state = ProcessState.ABORTED


            elif v.process_state == ProcessState.MEASURE_TONE0_TONE1:
                if v.tone0_tone1_mean is not None and \
                   v.tone0_tone1_sd is not None:
                    v.process_state = ProcessState.FADEOUT_TONE0_TONE1


            elif v.process_state == ProcessState.FADEOUT_TONE0_TONE1:
                if v.tones[0].inactive and v.tones[1].inactive:
                    v.process_state = ProcessState.DETECT_SILENCE


            elif v.process_state == ProcessState.DETECT_SILENCE:
                if v.detect_silence_detected:
                    v.process_state = ProcessState.MEASURE_SILENCE2

                if time.currentTime - v.state_start_time > v.detect_silence_max_time_in_state:
                    v.error = "Spent too long listening for silence (the second time)."
                    v.process_state = ProcessState.ABORTED

            elif v.process_state == ProcessState.MEASURE_SILENCE2:
                if v.silence2_mean is not None and \
                   v.silence2_sd is not None:
                    v.process_state = ProcessState.COMPLETED

            elif v.process_state == ProcessState.COMPLETING:
                v.process_state = ProcessState.COMPLETED


            elif v.process_state == ProcessState.COMPLETED:
                pass


            # Set state start time
            if previous_state != v.process_state:
                v.state_start_time = time.currentTime


            # States
            # ======

            if v.process_state == ProcessState.RESET:
                # It's a requirement that outdata is always actively
                # filled
                outdata.fill(0)

            elif v.process_state == ProcessState.MEASURE_SILENCE:
                # It's a requirement that outdata is always actively
                # filled
                outdata.fill(0)

                # Check if the sample is acceptable:
                if tones_level is not None and all(tones_level > 0):
                    # Levels are acceptable

                    # If we haven't started timing, do so now
                    if v.silence_start_time is None:
                        v.silence_start_time = time.inputBufferAdcTime

                    # Check if we've listened to enough silence
                    duration = time.currentTime - v.silence_start_time
                    if duration <= v.silence_threshold_duration:
                        # Record this level
                        v.silence_levels.append(tones_level)
                    elif len(v.silence_levels) < v.min_num_samples:
                        print("Insufficient samples of silence observed; listening for another half-second")
                        v.silence_threshold_duration += 0.5 # seconds
                    else:
                        # We've now collected enough sample levels;
                        # calculate the mean and standard deviation of the
                        # samples taken.
                        v.silence_mean = numpy.mean(v.silence_levels, axis=0)
                        v.silence_sd = numpy.std(v.silence_levels, axis=0)
                        print("silence_mean:",v.silence_mean)
                        print("silence_sd:", v.silence_sd)
                        print("sample size of silence:", len(v.silence_levels))


            elif v.process_state == ProcessState.FADEIN_TONE0:
                print("Fading in tone #0")
                v.tones[0].fadein(v.fadein_tone0_duration)
                v.tones[0].output(outdata)


            elif v.process_state == ProcessState.DETECT_TONE0:
                # Continue playing tone #0
                v.tones[0].output(outdata)

                # Calculate the number of standard deviations from silence
                num_sd = (tones_level[0] - v.silence_mean[0]) / v.silence_sd[0]

                if abs(num_sd) > v.non_silence_threshold_num_sd:
                    if v.non_silence_start_time is None:
                        v.non_silence_start_time = time.currentTime
                    else:
                        duration = time.currentTime - v.non_silence_start_time
                        if duration > v.non_silence_threshold_duration:
                            print("Non-silence detected")
                            v.non_silence_detected = True
                else:
                    # Reset timer
                    v.non_silence_start_time = None


            elif v.process_state == ProcessState.MEASURE_TONE0:
                # Continue playing tone #0
                v.tones[0].output(outdata)

                # Start the timer if not started
                if v.tone0_start_time is None:
                    v.tone0_start_time = time.currentTime

                # TODO: Should we check that we've got non-silence?

                # Save the levels because we assume we're hearing tone #0
                v.tone0_levels.append(tones_level)

                duration = time.currentTime - v.tone0_start_time
                if duration > v.tone0_threshold_duration:
                    if len(v.tone0_levels) >= v.min_num_samples:
                        # We've now collected enough samples
                        v.tone0_mean = numpy.mean(v.tone0_levels, axis=0)
                        v.tone0_sd = numpy.std(v.tone0_levels, axis=0)
                        print("tone0_mean:",v.tone0_mean)
                        print("tone0_sd:", v.tone0_sd)
                        print("tone0 number of samples:", len(v.tone0_levels))
                    else:
                        print("We haven't collected sufficient samples; increasing sampling time by half a second")
                        v.tone0_threshold_duration += 0.5


            elif v.process_state == ProcessState.DETECT_TONE1:
                # Continue playing tone #0
                v.tones[0].output(outdata)
                # Start playing tone #1
                v.tones[1].play()
                v.tones[1].output(outdata, op=operator.iadd)

                # Calculate the number of standard deviations from not-tone1
                num_sd = (tones_level[1] - v.tone0_mean[1]) / v.tone0_sd[1]

                if abs(num_sd) > v.detect_tone1_threshold_num_sd:
                    if v.detect_tone1_start_time is None:
                        v.detect_tone1_start_time = time.currentTime
                    else:
                        duration = time.currentTime - v.detect_tone1_start_time
                        if duration > v.detect_tone1_threshold_duration:
                            print("Tone #1 detected")
                            v.detect_tone1_detected = True
                else:
                    # Reset timer
                    v.non_silence_start_time = None


            elif v.process_state == ProcessState.MEASURE_TONE0_TONE1:
                # Continue playing tones
                v.tones[0].output(outdata)
                v.tones[1].output(outdata, op=operator.iadd)

                # Start the timer if not started
                if v.tone0_tone1_start_time is None:
                    v.tone0_tone1_start_time = time.currentTime

                # TODO: Should we check that we've got not-not-tone1?

                # Save the levels because we assume we're hearing tone #1
                v.tone0_tone1_levels.append(tones_level)

                duration = time.currentTime - v.tone0_tone1_start_time
                if duration > v.tone0_tone1_threshold_duration:
                    if len(v.tone0_tone1_levels) >= v.min_num_samples:
                        # We've now collected enough samples
                        v.tone0_tone1_mean = numpy.mean(v.tone0_tone1_levels, axis=0)
                        v.tone0_tone1_sd = numpy.std(v.tone0_tone1_levels, axis=0)
                        print("tone0_tone1_mean:",v.tone0_tone1_mean)
                        print("tone0_tone1_sd:", v.tone0_tone1_sd)
                        print("tone0_tone1 number of samples:", len(v.tone0_tone1_levels))
                    else:
                        print("We haven't collected sufficient samples; increasing sampling time by half a second")
                        v.tone0_tone1_threshold_duration += 0.5


            elif v.process_state == ProcessState.FADEOUT_TONE0_TONE1:
                print("Fading out tone #0")
                v.tones[0].fadeout(50/1000)
                
                print("Fading out tone #1")
                v.tones[1].fadeout(50/1000)

                v.tones[0].output(outdata)
                v.tones[1].output(outdata, op=operator.iadd)

                

            elif v.process_state == ProcessState.DETECT_SILENCE:
                # Actively fill the output buffer
                outdata.fill(0)

                # Calculate the number of standard deviations from not-tone0-tone1
                num_sd = (tones_level - v.tone0_tone1_mean) / v.tone0_tone1_sd

                if all(abs(num_sd) > v.detect_silence_threshold_num_sd):
                    if v.detect_silence_start_time is None:
                        v.detect_silence_start_time = time.currentTime #FIXME: is that the right time?
                    else:
                        duration = time.currentTime - v.detect_silence_start_time # FIXME: Is that the right time?
                        if duration > v.detect_silence_threshold_duration:
                            print("Silence detected")
                            v.detect_silence_detected = True
                else:
                    # Reset timer
                    v.detect_silence_start_time = None

                    
            elif v.process_state == ProcessState.MEASURE_SILENCE2:
                # Actively fill the output buffer
                outdata.fill(0)
                
                #print("Measuring silence #2")
                # Start the timer if not started
                if v.measure_silence2_start_time is None:
                    v.measure_silence2_start_time = time.currentTime

                # TODO: Should we check that we've got not-not-tone1?

                # Save the levels because we assume we're hearing silence
                max_samples = samples
                if v.measure_silence2_pcm_pos+max_samples > len(v.measure_silence2_pcm):
                    max_samples = len(v.measure_silence2_pcm) - v.measure_silence2_pcm_pos
                v.measure_silence2_pcm[
                    v.measure_silence2_pcm_pos:
                    v.measure_silence2_pcm_pos+max_samples] = \
                        indata[:max_samples]
                v.measure_silence2_pcm_pos += max_samples

                # Check if we've filled the buffer
                if v.measure_silence2_pcm_pos >= len(v.measure_silence2_pcm):
                    # Calculate the mean and std dev
                    v.silence2_mean = numpy.mean(
                        abs(v.measure_silence2_pcm)
                    )
                    v.silence2_sd = numpy.std(
                        v.measure_silence2_pcm
                    )
                    print("silence2_mean:", v.silence2_mean)
                    print("silence2_sd:", v.silence2_sd)
                    print("number of samples:", len(v.measure_silence2_pcm))

                
            elif v.process_state == ProcessState.COMPLETED:
                # Actively fill the output buffer
                outdata.fill(0)
                print("Successfully completed measuring levels")
                raise sd.CallbackStop


            elif v.process_state == ProcessState.ABORTED:
                outdata.fill(0)
                print("Aborting measuring levels: "+v.error)
                raise sd.CallbackAbort


            # DEBUG
            # Save outdata for analysis
            v.out_pcm[v.out_pcm_pos:v.out_pcm_pos+samples] = outdata[:]
            v.out_pcm_pos += samples
            if v.out_pcm_pos > len(v.out_pcm):
                v.error = "Buffer for PCM of output is full"
                raise sd.CallbackAbort

            if stream.cpu_load > 0.25:
                print("High CPU usage:", round(stream.cpu_load,3))

        except sd.CallbackAbort:
            raise

        except sd.CallbackStop:
            raise
                
        except Exception as exception:
            v.error = "Exception raised within callback: "+str(exception)
            v.exception = exception
            raise sd.CallbackAbort
            

        
    # Open a read-write stream
    stream = sd.Stream(samplerate=samples_per_second,
                       channels=channels,
                       dtype=numpy.float32,
                       latency=desired_latency,
                       callback=callback,
                       finished_callback=v.event.set)

    # Share reference to stream with callback
    v.stream = stream

    print("Measuring levels...")
    with stream:
        v.event.wait()  # Wait until measurement is finished

    # Save out pcm for analysis
    if v.out_pcm_pos > 0:
        wave_file = wave.open("out.wav", "wb")
        wave_file.setnchannels(output_channels)
        wave_file.setsampwidth(2)
        wave_file.setframerate(samples_per_second)
        data = v.out_pcm[0:v.out_pcm_pos]
        data = data * 2**15-1
        data = data.astype(numpy.int16)
        wave_file.writeframes(data)
        wave_file.close()
    else:
        print("WARNING: No data to write to out.wav")

    # Ensure that we have valid results
    if v.exception:
        print("ERROR: Exception caught in callback:")
        raise v.exception
    
    if v.error is not None:
        raise Exception("Failed to detect levels of the different tones: "+v.error)
    
    # Done!
    print("Finished measuring levels.")

    return {
        "tone0_mean": v.tone0_mean,
        "tone0_sd": v.tone0_sd,
        "tone0_tone1_mean": v.tone0_tone1_mean,
        "tone0_tone1_sd": v.tone0_tone1_sd,
        "silence2_mean": v.silence2_mean,
        "silence2_sd": v.silence2_sd
    }


if __name__ == "__main__":
    print("")
    print("Measuring Levels")
    print("================")
    print("")
    print("We are now going to measure the levels heard by your microphone when we play some tones.")
    print("These levels will be used later, when measuring the latency in your system.")
    print("")
    print("You will need to adjust the position of the microphone and output device.  For this")
    print("measurement, the microphone needs to be able to hear the output.  So, for example, if")
    print("you have headphones with an inline microphone, place the microphone over one of the")
    print("ear pieces.")
    print("")
    print("The process takes a few seconds.  It will first record the background noise, then it will")
    print("play a couple of tones.  You will need to ensure the volume is sufficiently high so that")
    print("the microphone can reliably hear the output.")
    print("")
    print("Do not move the microphone or output device once the system can hear the constant tone.")
    print("Do not make any noise during this measurement.")
    print("")
    print("Press enter to start.")
    print("")

    input() # wait for enter key

    levels = measure_levels(
        desired_latency="high"
        #desired_latency="low"
    )
    print(levels)
