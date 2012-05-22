#!/usr/bin/env python
import drmaa
import os
import sys
import glob
import time
import yaml
import subprocess
import copy
import tempfile
from optparse import OptionParser

import bcbio.solexa.flowcell
import bcbio.solexa.samplesheet

# The directory where CASAVA has written the demuxed output
CASAVA_OUTPUT_DIR = "Unaligned"
# If True, will launch a separate analysis process for each sample. 
# If False, will launch one analysis process for each project.
RUN_PIPELINE_BY_SAMPLE = True
# The analysis script for running the pipeline in parallell mode (on one node)  
PARALLELL_ANALYSIS_SCRIPT="automated_initial_analysis.py"
# The analysis script for running the pipeline in distributed mode (across multiple nodes/cores)
DISTRIBUTED_ANALYSIS_SCRIPT="distributed_nextgen_pipeline.py"
# The toplevel analysis directory.Project-specific directories will be written beneath
#ANALYSIS_DIR="/proj/a2010002/nobackup/illumina/"
ANALYSIS_DIR="/proj/a2010002/nobackup/illumina/pipeline_test/archive/casava_analysis"
# Not implemented in the drmaa module
EMAIL="seqmaster@scilifelab.se"
# For non-CASAVA analysis, this script is used to sanitize the run_info.yaml configuration file
PROCESS_YAML_SCRIPT = "process_run_info.py"
# If True, will sanitize the run_info.yaml configuration file when running non-CASAVA analysis
PROCESS_YAML = True
# If True, will assign the distributed master process and workers to a separate RabbitMQ queue for each flowcell 
FC_SPECIFIC_AMPQ = True


def main(post_process_config_file, fc_dir, run_info_file=None, only_run=False, only_setup=False, ignore_casava=False):
 
    run_arguments = [os.getcwd(),post_process_config_file,fc_dir,run_info_file]
    if has_casava_output(fc_dir) and not ignore_casava:
        if not only_run:
            sample_run_arguments, project_run_arguments = setup_analysis_directory_structure(post_process_config_file, fc_dir, run_info_file, ANALYSIS_DIR)
            if RUN_PIPELINE_BY_SAMPLE:
                run_arguments = sample_run_arguments
            else:
                run_arguments = project_run_arguments 
    else:
        if not only_run:
            run_arguments = setup_analysis(post_process_config_file, fc_dir, run_info_file, ANALYSIS_DIR)
    
    if not only_setup:
        for arguments in run_arguments:
            run_analysis(*arguments)
 
def run_analysis(work_dir, post_process, fc_dir, run_info):
    """Changes into the supplied work_dir directory and submits 
        the job using the supplied arguments and with slurm parameters
        obtained from the 'slurm_arguments' function.
    """
    
    # Move to the working directory
    start_dir = os.getcwd()
    os.chdir(work_dir)
    
    # Get the slurm arguments
    slurm_args = slurm_arguments(post_process)
    
    print "Initializing session"
    s = drmaa.Session()
    s.initialize()

    jt = s.createJobTemplate()
    jt.remoteCommand = slurm_args['script']
    args = [post_process,fc_dir]
    if run_info is not None:
        args.append(run_info)
    jt.args = args
    
    # TODO: job name is always (null), must fix slurm_drmaa C library and its
    # custom parsing (substitute "slurmdrmaa_parse_native"
    # for GNU GetOpt on slurm_drmaa/util.c)
    #jt.job_name = run_name
    #jt.blockEmail = 0
    #jt.mail = EMAIL
    #output = os.path.join(work_dir,"%s.out" % run_name)
    #error = os.path.join(work_dir,"%s.err" % run_name)
    #print "Setting output and error paths (%s,%s)" % (output,error)
    
    #jt.outputPath = output
    #jt.errorPath = error 
    jt.nativeSpecification = "-t %s -A %s -p %s %s" % (slurm_args['time'],slurm_args['project'],slurm_args['partition'],slurm_args.get('extra',""))

    print "Submitting job"
    jobid = s.runJob(jt)
    print 'Your job has been submitted with id ' + jobid

    s.deleteJobTemplate(jt)
    s.exit()
    
    # Change back to the starting directory
    os.chdir(start_dir)

def setup_analysis(post_process_config, archive_dir, run_info_file, analysis_dir):
    """Does a non-casava pre-analysis setup and returns a list of arguments
       that can be passed to the run_analysis function in order to start the
       analysis.
    """
    
    # Set the barcode type in run_info.yaml to "illumina", strip the 7th nucleotide and set analysis to 'Minimal'
    if run_info_file is not None and PROCESS_YAML:
        print "---------\nProcessing run_info:"
        run_info_backup = "%s.orig" % run_info_file
        os.rename(run_info_file,run_info_backup)
        cl = ["%s" % PROCESS_YAML_SCRIPT,run_info_backup,"--analysis","Align_illumina","--out_file",run_info_file,"--ascii","--clear_description"]
        print subprocess.check_output(cl)
        print "\n---------\n"
    
    # Check that the specified paths exist
    print "Checking input paths"
    for path in (post_process_config,archive_dir,run_info_file):
        if path is not None and not os.path.exists(path):
            raise Exception("The path %s does not exist" % path)
 
    print "Getting base_dir from %s" % post_process_config
    # Parse the config to get the analysis directory
    with open(post_process_config) as ppc:
        config = yaml.load(ppc)
    
    analysis = config.get("analysis",{})
    base_dir = analysis.get("base_dir",analysis_dir)
    
    print "Getting run name from %s" % archive_dir
    # Get the run name from the archive dir
    _,run_name = os.path.split(os.path.normpath(archive_dir))

    # Create the working directory if necessary and change into it
    work_dir = os.path.join(base_dir,run_name)
    os.chdir(base_dir)
    print "Creating/changing to %s" % work_dir
    try:
        os.mkdir(run_name,0770)
    except OSError:
        pass
    os.chdir(run_name)
 
    # make sure that the work dir exists
    if not os.path.exists(work_dir):
        raise Exception("The path %s does not exist and was not created" % work_dir)
    
    # if required, parse the machine id and flowcell position and use an ampq vhost specific for it
    if FC_SPECIFIC_AMPQ:
        machine_id = None
        flowcell_position = None
        for p in run_name.upper().split("_"):
            if p.startswith("SN"):
                machine_id = p
            elif p[0] in ("A","B") and p.endswith("XX"):
                flowcell_position = p[0]
        assert machine_id and flowcell_position, "Machine id and flowcell position could not be parsed from run name '%s'" % run_name
        
        # write a dedicated post_process.yaml for the ampq queue
        if config.get("distributed",False):
            config["distributed"]["rabbitmq_vhost"] = "bionextgen-%s-%s" % (machine_id,flowcell_position)
        
        post_process_config_orig = post_process_config
        parts = os.path.splitext(post_process_config)
        post_process_config = "%s-%s-%s%s" % (parts[0],machine_id,flowcell_position,parts[1])
        
        with open(post_process_config,"w") as fh:
            fh.write(yaml.safe_dump(config, default_flow_style=False, allow_unicode=True, width=1000)) 
            
    return [[os.getcwd(),post_process_config,archive_dir,run_info_file]]
        
def setup_analysis_directory_structure(post_process_config_file, fc_dir, custom_config_file, analysis_dir):
    """Parse the CASAVA 1.8+ generated flowcell directory and create a 
       corresponding directory structure suitable for bcbb analysis,
       complete with sample-specific and project-specific configuration files.
       Returns a list of arguments, both sample- and project-specific, that can 
       be passed to the run_analysis method for execution
    """
    
    assert os.path.exists(fc_dir), "ERROR: Flowcell directory %s does not exist" % fc_dir
    assert os.path.exists(analysis_dir), "ERROR: Analysis top directory %s does not exist" % analysis_dir
    
    # A list with the arguments to each run, both when running by sample and by project
    sample_run_arguments = []
    project_run_arguments = []
    
    # Parse the flowcell dir
    fc_dir_structure = parse_casava_directory(fc_dir)
    [fc_date, fc_name] = [fc_dir_structure['fc_date'],fc_dir_structure['fc_name']]
    fc_run_id = "%s_%s" % (fc_date,fc_name)
    
    # Parse the custom_config_file
    custom_config = []
    if custom_config_file is not None:
        with open(custom_config_file) as fh:
            custom_config = yaml.load(fh)
    
    # Iterate over the projects in the flowcell directory
    for project in fc_dir_structure.get('projects',[]):
        # Create a project directory if it doesn't already exist
        project_name = project['project_name']
        project_dir = os.path.join(analysis_dir,project_name)
        if not os.path.exists(project_dir):
            os.mkdir(project_dir,0770)
        
        # Collect the individual sample configs
        project_configs = []
        
        # Iterate over the samples in the project
        for sample_no, sample in enumerate(project.get('samples',[])):
            # Create a directory for the sample if it doesn't already exist
            sample_name = sample['sample_name']
            sample_dir = os.path.join(project_dir,sample_name)
            if not os.path.exists(sample_dir):
                os.mkdir(sample_dir,0770)
            
            # Create a directory for the flowcell if it does not exist
            dst_sample_dir = os.path.join(sample_dir,fc_run_id)
            if not os.path.exists(dst_sample_dir):
                os.mkdir(dst_sample_dir,0770)
            
            # rsync the source files to the sample directory
            src_sample_dir = os.path.join(fc_dir_structure['fc_dir'],fc_dir_structure['data_dir'],project['project_dir'],sample['sample_dir'])
            sample_files = do_rsync([os.path.join(src_sample_dir,f) for f in sample.get('files',[])],dst_sample_dir)
            
            # Generate a sample-specific configuration yaml structure
            sample_config = bcbb_configuration_from_samplesheet(os.path.join(src_sample_dir,sample['samplesheet'])) 
            # Append the sequence files to the config
            for lane in sample_config:
                if 'multiplex' in lane:
                    for sample in lane['multiplex']:
                        sample['files'] = [os.path.join(os.path.abspath(dst_sample_dir),os.path.basename(f)) for f in sample_files if f.find("_%s_L00%d_" % (sample['sequence'],int(lane['lane']))) >= 0]
                else:
                    lane['files'] = [os.path.join(os.path.abspath(dst_sample_dir),os.path.basename(f)) for f in sample_files if f.find("_L00%d_" % int(lane['lane'])) >= 0]
                    
            sample_config = override_with_custom_config(sample_config,custom_config)
            
            arguments = _setup_config_files(dst_sample_dir,sample_config,post_process_config_file,sample_name,fc_date,fc_name)
            project_configs.append(sample_config[0])
            sample_run_arguments.append([arguments[1],arguments[0],".",arguments[2]])
        
        # Write config files to the project directory
        arguments = _setup_config_files(project_dir,project_configs,post_process_config_file,"%s_%s_%s" % (fc_date,fc_name,project_name),fc_date,fc_name)
        project_run_arguments.append([arguments[1],arguments[0],".",arguments[2]])
    
    return sample_run_arguments, project_run_arguments

def override_with_custom_config(org_config, custom_config):
    """Override the default configuration from the .csv samplesheets
       with a custom configuration. Will replace overlapping options
       or add options that are missing from the samplesheet-generated
       config.
    """
    
    new_config = copy.deepcopy(org_config)
    
    for item in new_config:
        for custom_item in custom_config:
            if item['lane'] != custom_item.get('lane',""):
                continue
            for key, val in custom_item.items():
                if key == 'multiplex':
                    continue
                item[key] = val
                
            for sample in item.get('multiplex',[]):
                if 'sample_prj' not in sample or 'name' not in sample:
                    continue
                for custom_sample in custom_item.get('multiplex',[]):
                    if sample['sample_prj'] == custom_sample.get('sample_prj',"") and sample['name'] == custom_sample.get('name',""):
                        for key, val in custom_sample.items():
                            sample[key] = val
                        break
            break
        
    return new_config
       
def _setup_config_files(dst_dir,configs,post_process_config_file,sample_name="run",fc_date=None,fc_name=None):
    
    # Setup the data structure
    config_data_structure = {'details': configs}
    if fc_date is not None:
        config_data_structure['fc_date'] = fc_date
    if fc_name is not None:
        config_data_structure['fc_name'] = fc_name
        
    # Dump the config to file
    config_file = os.path.join(dst_dir,"%s-bcbb-config.yaml" % sample_name)
    with open(config_file,'w') as fh:
        fh.write(yaml.safe_dump(config_data_structure, default_flow_style=False, allow_unicode=True, width=1000))
            
    # Copy post-process file
    with open(post_process_config_file) as fh:
        local_post_process = yaml.load(fh) 
    # Update galaxy config to point to the original location
    local_post_process['galaxy_config'] = bcbio.utils.add_full_path(local_post_process['galaxy_config'],os.path.abspath(os.path.dirname(post_process_config_file)))
    local_post_process_file = os.path.join(dst_dir,"%s-post_process.yaml" % sample_name)
    with open(local_post_process_file,'w') as fh:
        fh.write(yaml.safe_dump(local_post_process, default_flow_style=False, allow_unicode=True, width=1000))
            
    # Write the command for running the pipeline with the configuration files
    run_command_file = os.path.join(dst_dir,"%s-bcbb-command.txt" % sample_name)
    with open(run_command_file,"w") as fh:
        fh.write(" ".join([os.path.basename(__file__),"--only-run",os.path.basename(local_post_process_file), ".", os.path.basename(config_file)])) 
        fh.write("\n")   
    
    return [os.path.basename(local_post_process_file), dst_dir, os.path.basename(config_file)]
    
def bcbb_configuration_from_samplesheet(csv_samplesheet):
    """Parse an illumina csv-samplesheet and return a dictionary suitable for the bcbb-pipeline
    """
    tfh, yaml_file = tempfile.mkstemp('.yaml','samplesheet')
    os.close(tfh)
    yaml_file = bcbio.solexa.samplesheet.csv2yaml(csv_samplesheet,yaml_file)
    with open(yaml_file) as fh:
        config = yaml.load(fh)
    
    # Replace the default analysis
    for lane in config:
        lane['analysis'] = 'Align_standard'
    
    # Remove the yaml file, we will write a new one later
    os.remove(yaml_file)
    
    return config
                
def do_rsync(src_files, dst_dir):
    cl = ["rsync","-ca"]
    cl.extend(src_files)
    cl.append(dst_dir)
    cl = [str(i) for i in cl]
    # For now, just touch the files rather than copy them
    # for f in src_files:
    #    open(os.path.join(dst_dir,os.path.basename(f)),"w").close()
    subprocess.check_call(cl)
    
    return [os.path.join(dst_dir,os.path.basename(f)) for f in src_files]
        
def parse_casava_directory(fc_dir):
    """Traverse a CASAVA 1.8+ generated directory structure and return a dictionary
    """ 
    projects = []
    
    fc_dir = os.path.abspath(fc_dir)
    fc_name, fc_date = bcbio.solexa.flowcell.get_flowcell_info(fc_dir)
    unaligned_dir = os.path.join(fc_dir,CASAVA_OUTPUT_DIR)
    project_dir_pattern = os.path.join(unaligned_dir,"Project_*")
    for project_dir in glob.glob(project_dir_pattern):
        project_samples = []
        sample_dir_pattern = os.path.join(project_dir,"Sample_*")
        for sample_dir in glob.glob(sample_dir_pattern):
            fastq_file_pattern = os.path.join(sample_dir,"*.fastq.gz")
            samplesheet_pattern = os.path.join(sample_dir,"*.csv")
            fastq_files = [os.path.basename(file) for file in glob.glob(fastq_file_pattern)]
            samplesheet = glob.glob(samplesheet_pattern)
            assert len(samplesheet) == 1, "ERROR: Could not unambiguously locate samplesheet in %s" % sample_dir
            sample_name = sample_dir.replace(sample_dir_pattern[0:-1],'')
            project_samples.append({'sample_dir': os.path.relpath(sample_dir,project_dir), 'sample_name': sample_name, 'files': fastq_files, 'samplesheet': os.path.basename(samplesheet[0])})
        project_name = project_dir.replace(project_dir_pattern[0:-1],'')
        projects.append({'project_dir': os.path.relpath(project_dir,unaligned_dir), 'project_name': project_name, 'samples': project_samples})
    return {'fc_dir': fc_dir, 'fc_name': fc_name, 'fc_date': fc_date, 'data_dir': os.path.relpath(unaligned_dir,fc_dir), 'projects': projects}
    
def has_casava_output(fc_dir):
    try:
        structure = parse_casava_directory(fc_dir)
        if len(structure['projects']) > 0:
            return True
    except:
        pass
    return False

def slurm_arguments(config_file):
    
    slurm_args = {}
    config = {}
    with open(config_file) as fh:
        config = yaml.load(fh)
    
    analysis_script = PARALLELL_ANALYSIS_SCRIPT    
    num_cores = config['algorithm'].get('num_cores',1)
    if num_cores == 'messaging':
        analysis_script = DISTRIBUTED_ANALYSIS_SCRIPT
    
    slurm_args['script'] = analysis_script
    slurm_args['project'] = 'a2010002'
    slurm_args['partition'] = 'core'
    slurm_args['time'] = '168:00:00'
    slurm_args['extra'] = ['--qos=seqver']

    return slurm_args

if __name__ == "__main__":

    parser = OptionParser()
    parser.add_option("-r", "--only-run", dest="only_run", action="store_true", default=False)
    parser.add_option("-s", "--only-setup", dest="only_setup", action="store_true", default=False)
    parser.add_option("-i", "--ignore-casava", dest="ignore_casava", action="store_true", default=False)
    options, args = parser.parse_args()
    
    if len(args) < 2:
        print __doc__
        sys.exit()
    
    run_info_file = None
    if len(args) > 2:
        run_info_file = args[2]
        
    main(args[0],args[1],run_info_file,options.only_run,options.only_setup,options.ignore_casava)
