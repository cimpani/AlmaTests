#!/bin/bash
echo "==START=="
/bin/hostname
echo "======="
/bin/ls -la
echo "======="
/bin/date
echo "======="

echo "kernel version check:"
uname -r

echo "Script: ${0##*/}"
echo "Job ID: $1"
echo "Data: $3"
echo "Processors: ${OMP_NUM_THREADS}";

echo "printing singularity version on grid:"
singularity --version
unset LD_LIBRARY_PATH

# ========================================================
# extract data:
COMMAND="ls $3_CalibratedData.tgz"
for FILE in `eval $COMMAND`
do
tar -xzvf $FILE > .tmp
done
/bin/ls -la $3_CalibratedData/
echo ">>> data set successfully extracted"

# ========================================================
# extract config files:
tar -xvzf configfiles.tar.gz > .tmp
/bin/ls -la *_config.txt
echo ">>> config files successfully extracted"

# ========================================================
# run script:
config=$3_config.txt
/bin/mkdir imaging_output
time singularity exec --cleanenv -H $PWD:/srv --pwd /srv -C rascil-latest.img python3 alma_rascil.py --config $config 

# ========================================================
# create outputs:

#tar -cvzf $3_outputFITS.tar.gz ./imaging_output/*.fits > .tmp
#tar -cvzf $3_outputPNGS.tar.gz ./imaging_output/*.png > .tmp

#tar -cvzf $3_outputFITS.tar.gz *.fits
#tar -cvzf $3_outputPNGS.tar.gz *.png 

tar czf $3_outputFITS.tar  ./imaging_output/*.fits
tar czf $3_outputPNGS.tar  ./imaging_output/*.png


/bin/ls -la
