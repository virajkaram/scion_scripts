# Import the .NET class library
import clr

# Import python sys module
import sys

# Import os module
import os

# glob for finding the saved spe file
import glob

# argparse for command line arguments
import argparse

# numpy import
import numpy as np

# for writing the acquired image as a fits file
from astropy.io import fits
from datetime import datetime

# Import c compatible List and String
from System import String, Int32
from System.Collections.Generic import List
from System.IO import FileAccess
from System.Threading import AutoResetEvent

# Add needed dll references
sys.path.append(os.environ['LIGHTFIELD_ROOT'])
sys.path.append(os.environ['LIGHTFIELD_ROOT']+"\\AddInViews")
clr.AddReference('PrincetonInstruments.LightFieldViewV5')
clr.AddReference('PrincetonInstruments.LightField.AutomationV5')
clr.AddReference('PrincetonInstruments.LightFieldAddInSupportServices')

# PI imports
from PrincetonInstruments.LightField.Automation import Automation
from PrincetonInstruments.LightField.AddIns import CameraSettings
from PrincetonInstruments.LightField.AddIns import ExperimentSettings
from PrincetonInstruments.LightField.AddIns import DeviceType
from PrincetonInstruments.LightField.AddIns import TimeStamps

# Reuse the buffer-to-numpy conversion from the scioncontrol samples
from scioncontrol.synchronous_acquisition import convert_buffer
from time import sleep

def device_found(experiment):
    # Find connected camera device
    for device in experiment.ExperimentDevices:
        if (device.Type == DeviceType.Camera):
            return True

    # If connected device is not a camera inform the user
    print("Camera not found. Please add a camera and try again.")
    return False


def get_acquisition_time_from_filename(filepath):
    # LightField saves files as "<Year> <Month name> <Day> <Hour>_<Minute>_<Second>.spe"
    # e.g. "2026 August 19 13_22_13.spe"
    basename = os.path.splitext(os.path.basename(filepath))[0]
    return datetime.strptime(basename, "%Y %B %d %H_%M_%S")


def get_acquisition_time(experiment, dataset):
    # If the camera reports its own exposure-start timestamp, use that.
    # dataset.TimeStampOrigin (absolute) + metaData.ExposureStarted (offset)
    # gives the true acquisition time; otherwise fall back to local time.
    if experiment.Exists(CameraSettings.AcquisitionTimeStampingStamps):
        frame_meta_data = dataset.GetFrameMetaData(0)

        if (dataset.TimeStampOrigin is not None and
                frame_meta_data.ExposureStarted is not None):
            camera_time = dataset.TimeStampOrigin.Value + frame_meta_data.ExposureStarted.Value
            return datetime(
                camera_time.Year, camera_time.Month, camera_time.Day,
                camera_time.Hour, camera_time.Minute, camera_time.Second,
                camera_time.Millisecond * 1000)

    return datetime.now()


def build_fits_header(experiment, exposure_time_ms, acquisition_time, obs_type="UNKNOWN"):
    header = fits.Header()

    header["EXPTIME"] = (exposure_time_ms, "Exposure time (ms)")
    header["DATE-OBS"] = (acquisition_time.isoformat(), "Acquisition date/time")

    for device in experiment.ExperimentDevices:
        if device.Type == DeviceType.Camera:
            header["CAMMODEL"] = (str(device.Model), "Camera model")
            header["CAMSN"] = (str(device.SerialNumber), "Camera serial number")
            break

    if experiment.Exists(CameraSettings.AdcAnalogGain):
        header["GAIN"] = (experiment.GetValue(CameraSettings.AdcAnalogGain).value__, "ADC analog gain")

    if experiment.Exists(CameraSettings.SensorTemperatureReading):
        header["CCDTEMP"] = (experiment.GetValue(CameraSettings.SensorTemperatureReading), "Sensor temperature (C)")

    header["OBSTYPE"] = (obs_type, "Observation type")
    return header


def create_experiment():
    # Create the LightField Application (true for visible)
    # The 2nd parameter forces LF to load with no experiment
    auto = Automation(True, List[String]())

    application = auto.LightFieldApplication

    # Get experiment and file manager objects
    return application.Experiment, application.FileManager


def acquire_single_image(exposure_time_ms, experiment, file_manager, save_directory, obs_type="UNKNOWN"):
    if not device_found(experiment):
        raise RuntimeError("Camera not found. Please add a camera and try again.")

    print("Camera found.")

    # Set the exposure time
    if experiment.Exists(CameraSettings.ShutterTimingExposureTime):
        experiment.SetValue(
            CameraSettings.ShutterTimingExposureTime,
            float(exposure_time_ms))
        print("Exposure time set to %s ms" % exposure_time_ms)

    # Only acquire a single frame
    # experiment.SetValue(ExperimentSettings.AcquisitionFramesToStore, Int32(1))

    # # Turn on exposure-start time stamping, if the camera supports it
    # if experiment.Exists(CameraSettings.AcquisitionTimeStampingStamps):
    #     experiment.SetValue(
    #         CameraSettings.AcquisitionTimeStampingStamps,
    #         TimeStamps.ExposureStarted)
    #     print("Exposure-start time stamping enabled")


    try:
        # Acquire image, saved by LightField under its default file name
        print("Acquiring image...")
        experiment.Acquire()
    except Exception as e:
        print("Error during acquisition: %s" % str(e))
        # Wait for acquisition to complete
    
    print("Waiting {exposure_time_ms/1000.0} + 5 seconds for acquisition to complete...")
    sleep(exposure_time_ms / 1000.0 + 5)
    return save_last_image_fits(experiment, file_manager, exposure_time_ms, save_directory, obs_type)


def save_last_image_fits(experiment, file_manager, exposure_time_ms, save_directory, obs_type="UNKNOWN"):
    # Find the .spe file LightField just saved
    directory = experiment.GetValue(ExperimentSettings.FileNameGenerationDirectory)
    files = glob.glob(os.path.join(directory, "*.spe"))
    last_image_acquired = max(files, key=os.path.getctime)
    print("Reading saved file: %s" % last_image_acquired)

    # Open the saved file back up
    dataset = file_manager.OpenFile(last_image_acquired, FileAccess.Read)
    print("Read file")
    acquisition_time = get_acquisition_time_from_filename(last_image_acquired)
    print("Acquisition time: %s" % acquisition_time.isoformat())

    image_frame = dataset.GetFrame(0, 0)
    image_data = image_frame.GetData()

    # Convert the acquired .NET buffer into a numpy array
    image_array = convert_buffer(image_data, image_frame.Format)

    dataset.Dispose()

    image_array = image_array.reshape(512, 640)
    print("Image converted to numpy array with shape %s" % (image_array.shape,))

    header = build_fits_header(experiment, exposure_time_ms, acquisition_time, obs_type)

    # Write the acquired image to disk as a fits file, using the same
    # acquisition_time for both the DATE-OBS header and the filename
    os.makedirs(save_directory, exist_ok=True)
    filename = "scion_image_%s.fits" % acquisition_time.strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(save_directory, filename)
    fits.writeto(filepath, image_array, header=header, overwrite=True)
    print("Fits file written to: %s" % filepath)

    return image_array


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Acquire a single image from the Scion camera.")
    parser.add_argument("exposure_time_ms", type=float, help="Exposure time in milliseconds")
    parser.add_argument("save_directory", help="Directory to save the acquired image as a fits file")
    parser.add_argument("--obs_type", default="UNKNOWN", help="Observation type for FITS header (default: UNKNOWN)")
    args = parser.parse_args()

    experiment, file_manager = create_experiment()
    print("Created experiment object.")

    image_array = acquire_single_image(args.exposure_time_ms, experiment, file_manager, args.save_directory, args.obs_type)

    print("Acquired image with %d pixels" % image_array.size)

