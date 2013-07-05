#!/usr/bin/env python

import argparse
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import urlparse

import deploy_hdfs
import deploy_utils
import deploy_zookeeper

from deploy_utils import Log

# regionserver must start before master
ALL_JOBS = ["regionserver", "master"]

MASTER_JOB_SCHEMA = {
    # "param_name": (type, default_value)
    # type must be in {bool, int, float, str}
    # if default_value is None, it means it's NOT an optional parameter.
    "hdfs_root": (str, None),
}

REGIONSERVER_JOB_SCHEMA = {
}

HBASE_SERVICE_MAP = {
    "master": MASTER_JOB_SCHEMA,
    "regionserver": REGIONSERVER_JOB_SCHEMA,
}

JOB_MAIN_CLASS = {
    "master": "org.apache.hadoop.hbase.master.HMaster",
    "regionserver": "org.apache.hadoop.hbase.regionserver.HRegionServer",
}

SHELL_COMMAND_INFO = {
  "shell": ("org.jruby.Main", "run the HBase shell"),
  "ruby": ("org.jruby.Main", "run the ruby shell"),
  "hbck": ("org.apache.hadoop.hbase.util.HBaseFsck",
      "run the hbase 'fsck' tool"),
  "htck": ("com.xiaomi.infra.hbase.AvailabilityTool",
      "run the hbase table availability check tool"),
  "hlog": ("org.apache.hadoop.hbase.regionserver.wal.HLogPrettyPrinter",
      "write-ahead-log analyzer"),
  "hfile": ("org.apache.hadoop.hbase.io.hfile.HFile", "store file analyzer"),
  "version": ("org.apache.hadoop.hbase.util.VersionInfo", "print the version"),
}

def generate_hbase_site_dict(args, host, job_name):
  zk_job = args.zk_config.jobs["zookeeper"]
  zk_hosts = ",".join(zk_job.hosts.itervalues())

  master_job = args.hbase_config.jobs["master"]
  regionserver_job = args.hbase_config.jobs["regionserver"]

  cluster_name = args.hbase_config.cluster.name
  config_dict = {}
  config_dict.update(args.hbase_config.cluster.site_xml)
  config_dict.update(args.hbase_config.jobs[job_name].site_xml)
  config_dict.update({
      # config for hbase:
      "hbase.cluster.name": cluster_name,
      "hbase.rootdir": "%s/hbase/%s/" % (master_job.hdfs_root, cluster_name),
      "hbase.cluster.distributed": "true",
      "hbase.zookeeper.quorum": zk_hosts,
      "hbase.zookeeper.property.clientPort": zk_job.base_port + 0,
      "hbase.master.port": master_job.base_port + 0,
      "hbase.master.info.port": master_job.base_port + 1,
      "hbase.regionserver.port": regionserver_job.base_port + 0,
      "hbase.regionserver.info.port": regionserver_job.base_port + 1,
      "zookeeper.znode.parent": "/hbase/" + cluster_name,
      "hbase.master.allow.shutdown": "false",
  })

  if host:
    supervisor_client = deploy_utils.get_supervisor_client(host,
        "hbase", args.hbase_config.cluster.name, job_name)
    config_dict.update({
        "hbase.tmp.dir": supervisor_client.get_available_data_dirs()[0],
    })

  if args.hbase_config.cluster.enable_security:
    # config security
    username = args.hbase_config.cluster.kerberos_username
    config_dict.update({
        "hbase.security.authentication": "kerberos",
        "hbase.rpc.engine": "org.apache.hadoop.hbase.ipc.SecureRpcEngine",
        "hbase.regionserver.kerberos.principal": "%s/hadoop@%s" % (
          args.hbase_config.cluster.kerberos_username or "hbase",
          args.hbase_config.cluster.kerberos_realm),
        "hbase.regionserver.keytab.file": "%s/%s.keytab" % (
          deploy_utils.HADOOP_CONF_PATH, username),
        "hbase.master.kerberos.principal": "%s/hadoop@%s" % (
          args.hbase_config.cluster.kerberos_username or "hbase",
          args.hbase_config.cluster.kerberos_realm),
        "hbase.master.keytab.file": "%s/%s.keytab" % (
          deploy_utils.HADOOP_CONF_PATH, username),
        "hbase.zookeeper.property.authProvider.1":
          "org.apache.zookeeper.server.auth.SASLAuthenticationProvider",
        "hbase.zookeeper.property.kerberos.removeHostFromPrincipal": "true",
        "hbase.zookeeper.property.kerberos.removeRealmFromPrincipal": "true",
        "hbase.security.authorization": "true",
        "hbase.coprocessor.region.classes":
          "org.apache.hadoop.hbase.security.token.TokenProvider",
    })

  if args.hbase_config.cluster.enable_acl:
    # config acl
    config_dict.update({
        "hbase.coprocessor.master.classes":
          "org.apache.hadoop.hbase.security.access.AccessController",
        "hbase.coprocessor.region.classes":
          ("org.apache.hadoop.hbase.security.token.TokenProvider," +
           "org.apache.hadoop.hbase.security.access.AccessController"),
        "hbase.superuser": "hbase_admin",
    })
  return config_dict

def generate_hbase_site_xml(args, host, job_name):
  config_dict = generate_hbase_site_dict(args, host, job_name)
  local_path = "%s/site.xml.tmpl" % deploy_utils.get_template_dir()
  return deploy_utils.generate_site_xml(args, local_path, config_dict)

def generate_zk_jaas_config(args, host, job_name):
  if not args.hbase_config.cluster.enable_security:
    return ""

  username = args.hbase_config.cluster.kerberos_username
  header_line = "com.sun.security.auth.module.Krb5LoginModule required"
  config_dict = {
    "useKeyTab": "true",
    "useTicketCache": "false",
    "keyTab": "\"%s/%s.keytab\"" % (deploy_utils.HADOOP_CONF_PATH, username),
    "principal": "\"%s/hadoop\"" % (
        args.hbase_config.cluster.kerberos_username or "hbase"),
    "debug": "true",
    "storeKey": "true",
  }

  return "Client {\n  %s\n%s;\n};" % (header_line,
      "\n".join(["  %s=%s" % (key, value)
        for (key, value) in config_dict.iteritems()]))

def generate_metrics_config(args, host, job_name):
  job = args.hbase_config.jobs[job_name]

  supervisor_client = deploy_utils.get_supervisor_client(host,
      "hbase", args.hbase_config.cluster.name, job_name)

  ganglia_switch = "# "
  if args.hbase_config.cluster.ganglia_address:
    ganglia_switch = ""
  config_dict = {
      "job_name": job_name,
      "period": job.metrics_period,
      "data_dir": supervisor_client.get_log_dir(),
      "ganglia_address": args.hbase_config.cluster.ganglia_address,
      "ganglia_switch": ganglia_switch,
  }

  local_path = "%s/hadoop-metrics.properties.tmpl" % deploy_utils.get_template_dir()
  template = deploy_utils.Template(open(local_path, "r").read())
  return template.substitute(config_dict)

def generate_configs(args, host, job_name):
  job = args.hbase_config.jobs[job_name]
  core_site_xml = deploy_hdfs.generate_core_site_xml(args, job_name, "hbase",
      args.hbase_config.cluster.enable_security, job)
  hdfs_site_xml = deploy_hdfs.generate_hdfs_site_xml_client(args)
  hbase_site_xml = generate_hbase_site_xml(args, host, job_name)
  hadoop_metrics_properties = generate_metrics_config(args, host, job_name)
  zk_jaas_conf = generate_zk_jaas_config(args, host, job_name)
  configuration_xsl = open("%s/configuration.xsl" % deploy_utils.get_template_dir()).read()
  log4j_xml = open("%s/hbase/log4j.xml" % deploy_utils.get_template_dir()).read()
  krb5_conf = open("%s/krb5-hadoop.conf" % deploy_utils.get_config_dir()).read()

  config_files = {
    "core-site.xml": core_site_xml,
    "hdfs-site.xml": hdfs_site_xml,
    "hbase-site.xml": hbase_site_xml,
    "hadoop-metrics.properties": hadoop_metrics_properties,
    "jaas.conf": zk_jaas_conf,
    "configuration.xsl": configuration_xsl,
    "log4j.xml": log4j_xml,
    "krb5.conf": krb5_conf,
  }
  return config_files

def get_job_specific_params(args, job_name):
  return ""

def generate_run_scripts_params(args, host, job_name):
  job = args.hbase_config.jobs[job_name]

  supervisor_client = deploy_utils.get_supervisor_client(host,
      "hbase", args.hbase_config.cluster.name, job_name)

  artifact_and_version = "hbase-" + args.hbase_config.cluster.version

  component_dir = "$package_dir/"
  # must include both [dir]/ and [dir]/* as [dir]/* only import all jars under
  # this dir but we also need access the webapps under this dir.
  jar_dirs = "%s/:%s/lib/*:%s/*" % (component_dir, component_dir, component_dir)

  script_dict = {
      "artifact": artifact_and_version,
      "job_name": job_name,
      "jar_dirs": jar_dirs,
      "run_dir": supervisor_client.get_run_dir(),
      "params":
          '-Xmx%dm ' % job.xmx +
          '-Xms%dm ' % job.xms +
          '-Xmn%dm ' % job.xmn +
          '-Xss256k ' +
          '-XX:MaxDirectMemorySize=%dm ' % job.max_direct_memory +
          '-XX:MaxPermSize=%dm ' % job.max_perm_size +
          '-XX:PermSize=%dm ' % job.max_perm_size +
          '-XX:+HeapDumpOnOutOfMemoryError ' +
          '-XX:HeapDumpPath=$log_dir ' +
          '-XX:+PrintGCApplicationStoppedTime ' +
          '-XX:+UseConcMarkSweepGC ' +
          '-verbose:gc ' +
          '-XX:+PrintGCDetails ' +
          '-XX:+PrintGCDateStamps ' +
          '-Xloggc:$log_dir/%s_gc_${start_time}.log ' % job_name +
          '-XX:+UseMembar ' +
          '-XX:SurvivorRatio=1 ' +
          '-XX:+UseCMSCompactAtFullCollection ' +
          '-XX:CMSInitiatingOccupancyFraction=75 ' +
          '-XX:+UseCMSInitiatingOccupancyOnly ' +
          '-XX:+CMSParallelRemarkEnabled ' +
          '-XX:+UseNUMA ' +
          '-XX:+CMSClassUnloadingEnabled ' +
          '-XX:+PrintSafepointStatistics ' +
          '-XX:PrintSafepointStatisticsCount=1 ' +
          '-XX:+PrintHeapAtGC ' +
          '-XX:+PrintTenuringDistribution ' +
          '-XX:CMSMaxAbortablePrecleanTime=10000 ' +
          '-XX:TargetSurvivorRatio=80 ' +
          '-XX:+UseGCLogFileRotation ' +
          '-XX:NumberOfGCLogFiles=100 ' +
          '-XX:GCLogFileSize=128m ' +
          '-XX:CMSWaitDuration=2000 ' +
          '-XX:+CMSScavengeBeforeRemark ' +
          '-XX:+PrintClassHistogramAfterFullGC ' +
          '-XX:+PrintClassHistogramBeforeFullGC ' +
          '-XX:+PrintPromotionFailure ' +
          '-XX:ConcGCThreads=8 ' +
          '-XX:ParallelGCThreads=8 ' +
          '-XX:PretenureSizeThreshold=4m ' +
          '-XX:+CMSConcurrentMTEnabled ' +
          '-XX:+ExplicitGCInvokesConcurrent ' +
          '-XX:+SafepointTimeout ' +
          '-XX:MonitorBound=16384 ' +
          '-XX:OldPLABSize=16 ' +
          '-XX:-ResizeOldPLAB ' +
          '-XX:-UseBiasedLocking ' +
          '-Dproc_%s ' % job_name +
          '-Djava.net.preferIPv4Stack=true ' +
          '-Dhbase.log.dir=$log_dir ' +
          '-Dhbase.pid=$pid ' +
          '-Dhbase.cluster=%s ' % args.hbase_config.cluster.name +
          '-Dhbase.policy.file=hbase-policy.xml ' +
          '-Dhbase.home.dir=$package_dir ' +
          '-Djava.security.krb5.conf=$run_dir/krb5.conf ' +
          '-Dhbase.id.str=%s ' % args.remote_user +
          get_job_specific_params(args, job_name),
  }

  if args.hbase_config.cluster.enable_security:
    jaas_path = "%s/jaas.conf" % supervisor_client.get_run_dir()
    script_dict["params"] += "-Djava.security.auth.login.config=%s " % jaas_path
    boot_class_path = ("$package_dir/lib/hadoop-security-%s.jar" %
        args.hdfs_config.cluster.version)
    script_dict["params"] += "-Xbootclasspath/p:%s " % boot_class_path

  script_dict["params"] += JOB_MAIN_CLASS[job_name]
  return script_dict

def get_hbase_service_config(args):
  args.hbase_config = deploy_utils.get_service_config_full(
      args, HBASE_SERVICE_MAP)
  if not args.hbase_config.cluster.zk_cluster:
    Log.print_critical(
        "hdfs cluster must depends on a zookeeper clusters: %s" %
        args.hbase_config.cluster.name)

  hdfs_root = args.hbase_config.jobs["master"].hdfs_root
  url = urlparse.urlparse(hdfs_root)
  if url.scheme != "hdfs":
    Log.print_critical(
        "Only hdfs supported as data root: %s" % hdfs_root)
  args.hbase_config.jobs["master"].hdfs_root = hdfs_root.rstrip("/")

  hdfs_args = argparse.Namespace()
  hdfs_args.root = deploy_utils.get_root_dir("hdfs")
  hdfs_args.service = "hdfs"
  hdfs_args.cluster = url.netloc

  args.hdfs_config = deploy_utils.get_service_config(
      hdfs_args, deploy_hdfs.HDFS_SERVICE_MAP)

def generate_start_script(args, host, job_name):
  script_params = generate_run_scripts_params(args, host, job_name)
  script_params["params"] += " start"
  return deploy_utils.create_run_script(
      "%s/start.sh.tmpl" % deploy_utils.get_template_dir(),
      script_params)

def install(args):
  get_hbase_service_config(args)
  deploy_utils.install_service(args, "hbase", args.hbase_config, "hbase")

def cleanup(args):
  get_hbase_service_config(args)

  cleanup_token = deploy_utils.confirm_cleanup(args,
      "hbase", args.hbase_config)

  for job_name in args.job or ALL_JOBS:
    hosts = args.hbase_config.jobs[job_name].hosts
    for id in args.task or hosts.iterkeys():
      deploy_utils.cleanup_job("hbase", args.hbase_config,
          hosts[id], job_name, cleanup_token)

def bootstrap_job(args, host, job_name, cleanup_token):
  deploy_utils.bootstrap_job(args, "hbase", "hbase",
      args.hbase_config, host, job_name, cleanup_token, '0')
  start_job(args, host, job_name)

def bootstrap(args):
  get_hbase_service_config(args)

  cleanup_token = deploy_utils.confirm_bootstrap("hbase", args.hbase_config)

  for job_name in args.job or ALL_JOBS:
    hosts = args.hbase_config.jobs[job_name].hosts
    for id in args.task or hosts.iterkeys():
      bootstrap_job(args, hosts[id], job_name, cleanup_token)

def start_job(args, host, job_name):
  config_files = generate_configs(args, host, job_name)
  start_script = generate_start_script(args, host, job_name)
  http_url = 'http://%s:%d' % (host,
    args.hbase_config.jobs[job_name].base_port + 1)
  deploy_utils.start_job(args, "hbase", "hbase", args.hbase_config,
      host, job_name, start_script, http_url, **config_files)

def start(args):
  deploy_utils.confirm_start(args)
  get_hbase_service_config(args)

  for job_name in args.job or ALL_JOBS:
    hosts = args.hbase_config.jobs[job_name].hosts
    for id in args.task or hosts.iterkeys():
      start_job(args, hosts[id], job_name)

def stop_job(args, host, job_name):
  deploy_utils.stop_job("hbase", args.hbase_config,
      host, job_name)

def stop(args):
  deploy_utils.confirm_stop(args)
  get_hbase_service_config(args)

  for job_name in args.job or reversed(ALL_JOBS):
    hosts = args.hbase_config.jobs[job_name].hosts
    for id in args.task or hosts.iterkeys():
      stop_job(args, hosts[id], job_name)

def restart(args):
  deploy_utils.confirm_restart(args)
  get_hbase_service_config(args)

  for job_name in args.job or reversed(ALL_JOBS):
    hosts = args.hbase_config.jobs[job_name].hosts
    for id in args.task or hosts.iterkeys():
      stop_job(args, hosts[id], job_name)

  for job_name in args.job or ALL_JOBS:
    hosts = args.hbase_config.jobs[job_name].hosts
    for id in args.task or hosts.iterkeys():
      deploy_utils.wait_for_job_stopping("hbase",
          args.hbase_config.cluster.name, job_name, hosts[id])
      start_job(args, hosts[id], job_name)

def show(args):
  get_hbase_service_config(args)

  for job_name in args.job or ALL_JOBS:
    hosts = args.hbase_config.jobs[job_name].hosts
    for id in args.task or hosts.iterkeys():
      deploy_utils.show_job("hbase", args.hbase_config,
          hosts[id], job_name)

def run_shell(args):
  get_hbase_service_config(args)

  main_class, options = deploy_utils.parse_shell_command(
      args, SHELL_COMMAND_INFO)
  if not main_class:
    return

  core_site_dict = deploy_hdfs.generate_core_site_dict(args,
      "namenode", "hdfs", args.hdfs_config.cluster.enable_security)
  hdfs_site_dict = deploy_hdfs.generate_hdfs_site_dict_client(args)
  hbase_site_dict = generate_hbase_site_dict(args, "", "master")

  hbase_opts = list()
  for key, value in core_site_dict.iteritems():
    hbase_opts.append("-D%s%s=%s" % (deploy_utils.HADOOP_PROPERTY_PREFIX,
          key, value))
  for key, value in hdfs_site_dict.iteritems():
    hbase_opts.append("-D%s%s=%s" % (deploy_utils.HADOOP_PROPERTY_PREFIX,
          key, value))
  for key, value in hbase_site_dict.iteritems():
    hbase_opts.append("-D%s%s=%s" % (deploy_utils.HADOOP_PROPERTY_PREFIX,
          key, value))

  if args.hbase_config.cluster.enable_security:
    hbase_opts.append("-Djava.security.krb5.conf=%s/krb5-hadoop.conf" %
        deploy_utils.get_config_dir())

    (jaas_fd, jaas_file) = tempfile.mkstemp()
    os.write(jaas_fd, deploy_zookeeper.generate_client_jaas_config(args,
          deploy_utils.get_user_principal_from_ticket_cache()))
    os.close(jaas_fd)
    hbase_opts.append("-Djava.security.auth.login.config=%s" % jaas_file)

  package_root = deploy_utils.get_hbase_package_root(
      args.hbase_config.cluster.version)
  class_path = "%s/:%s/lib/*:%s/*" % (package_root, package_root, package_root)

  cmd = ["java", "-cp", class_path] + hbase_opts + [main_class]
  if args.command[0] == "shell":
    cmd += ["-X+O", "%s/bin/hirb.rb" % package_root]
  cmd += options
  p = subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)
  return p.wait()

def update_hbase_env_sh(args, artifact, version):
  current_path = os.path.abspath(os.path.dirname(
        os.path.realpath(args.package_root)))
  conf_path = "%s/%s/%s/%s-%s/conf" % (current_path, args.package_root,
    args.cluster, artifact, version)
  hbase_opts = "-Djava.security.auth.login.config=$HBASE_CONF_DIR/jaas.conf "
  hbase_opts += "-Djava.security.krb5.conf=$HBASE_CONF_DIR/krb5.conf"
  deploy_utils.append_to_file("%s/hbase-env.sh" % conf_path,
      'export HBASE_OPTS="$HBASE_OPTS %s"\n' % hbase_opts)

def generate_client_config(args, artifact, version):
  config_path = "%s/%s/%s-%s/conf" % (args.package_root,
      args.cluster, artifact, version)
  master_host = args.hbase_config.jobs["master"].hosts[0]
  config_path = "%s/%s/%s-%s/conf" % (args.package_root,
      args.cluster, artifact, version)
  deploy_utils.write_file("%s/hbase-site.xml" % config_path,
      generate_hbase_site_xml(args, master_host, "master"))
  deploy_utils.write_file("%s/hadoop-metrics.properties" % config_path,
      generate_metrics_config(args, master_host, "master"))
  deploy_utils.write_file("%s/core-site.xml" % config_path,
      deploy_hdfs.generate_core_site_xml(args, "namenode", "hbase",
        args.hbase_config.cluster.enable_security))
  deploy_utils.write_file("%s/hdfs-site.xml" % config_path,
      deploy_hdfs.generate_hdfs_site_xml_client(args))
  deploy_utils.write_file("%s/jaas.conf" % config_path,
      deploy_zookeeper.generate_client_jaas_config(args,
        deploy_utils.get_user_principal_from_ticket_cache()))
  deploy_utils.write_file("%s/krb5.conf" % config_path,
      open('%s/krb5-hadoop.conf' % deploy_utils.get_config_dir()).read())
  update_hbase_env_sh(args, artifact, version)

def pack(args):
  get_hbase_service_config(args)
  version = args.hbase_config.cluster.version
  deploy_utils.make_package_dir(args, "hbase", version)
  generate_client_config(args, "hbase", version)

  if not args.skip_tarball:
    deploy_utils.pack_package(args, "hbase", version)
  Log.print_success("Pack client utilities for hbase success!\n")

def vacate_region_server(args, ip):
  package_root = deploy_utils.get_hbase_package_root(
      args.hbase_config.cluster.version)
  Log.print_info("Vacate region server: " + ip);
  host = socket.gethostbyaddr(ip)[0]
  args.command = ["ruby", "%s/bin/region_mover.rb" % package_root,
    "unload", host]
  if run_shell(args) != 0:
    Log.print_critical("Unload host %s failed." % host);

def recover_region_server(args, ip):
  package_root = deploy_utils.get_hbase_package_root(
      args.hbase_config.cluster.version)
  Log.print_info("Recover region server: " + ip);
  host = socket.gethostbyaddr(ip)[0]
  args.command = ["ruby", "%s/bin/region_mover.rb" % package_root,
    "load", host]
  if run_shell(args) != 0:
    Log.print_critical("Load host %s failed." % host);

def balance_switch(args, flag):
  fd, filename = tempfile.mkstemp()
  f = os.fdopen(fd, 'w+')
  if flag:
    Log.print_info("balance_switch on for cluster: %s" % args.cluster)
    print >> f, 'balance_switch true'
  else:
    Log.print_info("balance_switch off for cluster: %s" % args.cluster)
    print >> f, 'balance_switch false'
  print >> f, 'exit'
  f.close()
  args.command = ["shell", filename]
  ret = run_shell(args)
  os.remove(filename)
  if ret != 0:
    Log.print_critical("balance_switch off for cluster: %s failed!" %
        args.cluster);

def rolling_update(args):
  if not args.job:
    Log.print_critical("You must specify the job name to do rolling update")

  if not args.skip_confirm:
    deploy_utils.confirm_action(args, "rolling_update")

  get_hbase_service_config(args)
  job_name = args.job[0]

  if job_name != 'regionserver':
    args.vacate_rs = False

  if args.vacate_rs:
    balance_switch(args, False)

  Log.print_info("Rolling updating %s" % job_name)
  hosts = args.hbase_config.jobs[job_name].hosts
  wait_time = 0

  for id in hosts.iterkeys():
    if not args.skip_confirm:
      deploy_utils.confirm_rolling_update(id, wait_time)

    if args.vacate_rs:
       vacate_region_server(args, hosts[id])

    stop_job(args, hosts[id], job_name)
    deploy_utils.wait_for_job_stopping("hbase",
        args.hbase_config.cluster.name, job_name, hosts[id])
    start_job(args, hosts[id], job_name)
    deploy_utils.wait_for_job_starting("hbase",
        args.hbase_config.cluster.name, job_name, hosts[id])

    if args.vacate_rs:
      recover_region_server(args, hosts[id])
    wait_time = args.time_interval

  if args.vacate_rs:
    balance_switch(args, True)
  Log.print_success("Rolling updating %s success" % job_name)

if __name__ == '__main__':
  test()
