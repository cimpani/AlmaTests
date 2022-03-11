#bin/bash
echo 'Checking the location of python on the grid';
which python;
echo "which version of python";
/usr/bin/python --version;
echo "==============================================";
echo "Print environment details";
printenv;
echo "==============================================";
singularity --version;

echo "Processors: ${OMP_NUM_THREADS}";

mkdir data

# ========================================================
# extract data:
COMMAND="ls $3_CalibratedData.tgz"
for FILE in `eval $COMMAND`
do
tar -xzvf $FILE  -C data
done
echo ">>> data set successfully extracted"

# ========================================================
# extract img  files:
tar -xvf imgfiles.tar 
/bin/ls -la *_Imaging.py
echo ">>> img files successfully extracted"


/bin/mv casa-5.4.1.simg  data/$3_CalibratedData
/bin/mv $3_Imaging.py  data/$3_CalibratedData


cd data
cd $3_CalibratedData


echo "print variables";
echo $HOME" PWD " $PWD"   pwd  "
#/bin/pwd

echo "=======";
/bin/ls -la;


COMMAND="ls $3_Imaging.py"
for FILE in `eval $COMMAND`
do
time singularity exec --cleanenv -H $PWD:/srv --pwd /srv -C casa-5.4.1.simg casa --nogui --log2term -c $FILE
done
echo ">>> casa script ran successfully"



# ========================================================
# print results inside data folder:
echo "print results inside data folder";
echo $HOME" PWD " $PWD"   pwd  "
echo "=======";
/bin/ls -ltra;



cd ../..
echo "print variables outside work dir";
echo $HOME" PWD " $PWD"   pwd  "
echo "=======";
/bin/ls -la;

echo "saving outputs"
tar czf $3_outputFITS.tar  data/$3_CalibratedData/*.fits
