Query Store:
------------

The Query Store module reads events from tez and hive and populates the database with the required info.

Build from source:
------------

mvn clean install

Running in dev mode:
------------

# Install postgres 9.6

brew install 'postgresql@9.6'

# Start & stop using qpctl

```
./qpctl start # Start postgres, setup database schema and start query processor

./qpctl stop # Stop query processor, delete schema and stop postgres.
```

There are other commands like jvstop and jvstart to only start and stop the query processor.

# Dev configurations, to work remote hdfs:

* Change fs.defaultFS in conf/core-site.xml to point to the correct hdfs server. Affects eventprocessor source and download logs.
* Change yarn.resourcemanager.webapp.address and yarn.timeline-service.webapp.address in conf/yarn-site.xml to point to correct RM and timeline address. Affects download logs.
* If we have to test download logs via hive set hue.query-processor.debug-bundler.logs-source to "k8s" and set hue.query-processor.hive-jdbc-url to correct jdbc url in conf/hue-query-processor.json.

This will work only with non secure cluster. For secure cluster, you have to get the core-site.xml from the cluster and setup kerberos and configure das with correct keytabs.

# Dev configuration, to work with localfile system:

* Delete conf/core-site.xml or remove fs.defaultFS entry in conf/core-site.xml.
* Change paths hive.hook.proto.base-directory and tez.history.logging.proto-base-dir in conf/hue-query-processor.json