import json
import os
from collections import OrderedDict
from tempfile import NamedTemporaryFile

import re

import datetime

from infra_buddy.aws import s3
from infra_buddy.context.service_definition import ServiceDefinition
from infra_buddy.context.template_manager import TemplateManager
from infra_buddy.utility import print_utility

DOCKER_REGISTRY = 'DOCKER_REGISTRY_URL'
ROLE = 'ROLE'
APPLICATION = 'APPLICATION'
ENVIRONMENT = 'ENVIRONMENT'
REGION = 'REGION'
SKIP_ECS = 'SKIP_ECS'
built_in = [DOCKER_REGISTRY, ROLE, APPLICATION, ENVIRONMENT, REGION, SKIP_ECS]
env_variables = OrderedDict()
env_variables['VPCAPP'] = "{VPCAPP}"
env_variables['DEPLOY_DATE'] = "{DEPLOY_DATE}"
env_variables['STACK_NAME'] = "{ENVIRONMENT}-{APPLICATION}-{ROLE}"
env_variables['EnvName'] = "{STACK_NAME}"  # alias
env_variables['ECS_SERVICE_STACK_NAME'] = "{STACK_NAME}"  # alias
env_variables['VPC_STACK_NAME'] = "{ENVIRONMENT}-{VPCAPP}-vpc"
env_variables['CF_BUCKET_NAME'] = "{ENVIRONMENT}-{VPCAPP}-cloudformation-deploy-resources"
env_variables['CF_DEPLOY_RESOURCE_PATH'] = "{STACK_NAME}/{DEPLOY_DATE}"
env_variables['CF_BUCKET_URL'] = "https://s3-{REGION}.amazonaws.com/{CF_BUCKET_NAME}"
env_variables['CLUSTER_STACK_NAME'] = "{ENVIRONMENT}-{APPLICATION}-cluster"
env_variables['RESOURCE_STACK_NAME'] = "{ENVIRONMENT}-{APPLICATION}-{ROLE}-resources"
env_variables['ECS_SERVICE_RESOURCE_STACK_NAME'] = "{RESOURCE_STACK_NAME}"  # alias
env_variables['KEY_NAME'] = "{ENVIRONMENT}-{APPLICATION}"
env_variables['CHANGE_SET_NAME'] = "{STACK_NAME}-deploy-cloudformation-change-set"


class DeployContext(dict):
    def __init__(self, defaults, environment):
        super(DeployContext, self).__init__()
        self['ENVIRONMENT'] = environment.lower()
        self.temp_files = []
        self._initalize_defaults(defaults)

    @classmethod
    def create_deploy_context_artifact(cls, artifact_directory, environment, defaults=None):
        # type: (str, str) -> DeployContext
        """
        :rtype DeployContext
        :param artifact_directory: Path to directory containing service definition.
                May be a s3 URL pointing at a zip archive
        :param defaults: Path to json file containing default environment settings
        """
        ret = DeployContext(defaults=defaults, environment=environment)
        ret._initialize_artifact_directory(artifact_directory)
        ret._initialize_environment_variables()
        return ret

    @classmethod
    def create_deploy_context(cls, application, role, environment, defaults=None):
        # type: (str, str, str, str) -> DeployContext
        """
        :rtype DeployContext
        :param application: Application name
        :param role: Role of service
        :param environment: Environment to deploy
        :param defaults: Path to json file containing default environment settings
        """
        ret = DeployContext(defaults=defaults, environment=environment)
        ret['APPLICATION'] = application
        ret['ROLE'] = role
        ret._initialize_environment_variables()
        return ret

    def _initialize_artifact_directory(self, artifact_directory):
        # type: (str) -> None
        if artifact_directory.startswith("s3://"):
            artifact_directory = s3.download_zip_from_s3_url(artifact_directory)
        service_definition = ServiceDefinition(artifact_directory, self['ENVIRONMENT'])
        self[APPLICATION] = service_definition.application
        self[ROLE] = service_definition.role
        self[DOCKER_REGISTRY] = service_definition.docker_registry
        self.update(service_definition.deployment_parameters)
        self.service_definition = service_definition
        image_definition = os.path.join(artifact_directory, "containerurl.txt")
        if os.path.exists(image_definition):
            with open(image_definition, 'r') as image:
                self['IMAGE'] = image.read()
        else:
            print_utility.warn("Image definition (containerurl.txt) did not exist in artifact directory.")

    def _initialize_environment_variables(self):
        application = self['APPLICATION']
        self['VPCAPP'] = application if '-' not in application else application[:application.find('-')]
        self['DEPLOY_DATE'] = datetime.datetime.now().strftime("%b_%d_%Y_Time_%H_%M")
        for property_name in built_in:
            self.__dict__[property_name.lower()] = self.get(property_name, None)
        for variable, template in env_variables.iteritems():
            evaluated_template = template.format(**self)
            self[variable] = evaluated_template
            self.__dict__[variable.lower()] = evaluated_template

    def _initalize_defaults(self, defaults):
        self.defaults = defaults
        if defaults:
            with open(defaults, 'r') as fp:
                config = json.load(fp)
                self.update(config)
        self.update(os.environ)
        self.template_manager = TemplateManager(self)

    def generate_modification_stack_name(self, mod_name):
        return "{ENVIRONMENT}-{APPLICATION}-{ROLE}-{mod_name}".format(mod_name=mod_name, **self)

    def generate_modification_resource_stack_name(self, mod_name):
        return "{ENVIRONMENT}-{APPLICATION}-{ROLE}-{mod_name}-resources".format(mod_name=mod_name, **self)

    def get_region(self):
        return self._get_required_default_configuration(REGION)

    def _get_required_default_configuration(self, key):
        region = self.get(key, os.environ.get(key, None))
        if not region:
            raise Exception("Required default not set {key}.\n"
                            "Configure --configuration-defaults or set ENVIRONMENT variable {key}".format(
                key=key))
        return region

    def notify_event(self, title, type, message=None):
        pass

    def get_service_modifications(self):
        return self.service_definition.service_modifications

    def should_skip_ecs_trivial_update(self):
        return self.get(SKIP_ECS, os.environ.get(SKIP_ECS, True))

    def render_template(self, file):
        with open(file, 'r') as source:
            with NamedTemporaryFile(delete=False) as destination:
                temp_file_path = os.path.abspath(destination.name)
                print temp_file_path
                self.temp_files.append(temp_file_path)
                for line in source:
                    destination.write(self._expandvars(line))
                return temp_file_path

    def __del__(self):
        for file in self.temp_files:
            os.remove(file)

    def get_execution_plan(self):
        return self.service_definition.generate_execution_plan(self.template_manager)

    def _expandvars(self, path, default=None, skip_escaped=False):
        """Expand ENVIRONMENT variables of form $var and ${var}.
           If parameter 'skip_escaped' is True, all escaped variable references
           (i.e. preceded by backslashes) are skipped.
           Unknown variables are set to 'default'. If 'default' is None,
           they are left unchanged.
        """

        def replace_var(m):
            return self.get(m.group(2) or m.group(1), m.group(0) if default is None else default)

        reVar = (r'(?<!\\)' if skip_escaped else '') + r'\$(\w+|\{([^}]*)\})'
        sub = re.sub(reVar, replace_var, path)
        return sub

        # rerun otx:notify-datadog \
        #     --title "${ACTION} stack ${STACK_NAME} started"   \
        #     --message "The ${ACTION} of stack ${STACK_NAME} has been started" \
        #     --type success \
        #     --tags "application:${application} ROLE:${ROLE} ENVIRONMENT:${ENVIRONMENT} system:${application}-${ROLE}"



        #
        # function parse_template() {
        #     perl -p -i -e 's/\$\{([^}]+)\}/defined $ENV{$1} ? $ENV{$1} : $&/eg' \
        #         <  $1 \
        #         > $2 \
        #         2> /dev/null
        # }
        #
        # function does_stack_exist(){
        #     if [[ $(get_cloudformation_stack_status ${1}) ]]; then
        #         echo "Yes"
        #     else
        #         echo "No"
        #     fi
        # }
        # function print_stack_param(){
        #     if [[ $(does_stack_exist ${1}) == "Yes" ]]; then
        #         aws cloudformation describe-stacks  --stack-name ${1} |  jq -r --arg parameter_key "${2}" '.Stacks[].Parameters[] | select(.ParameterKey == $parameter_key) | .ParameterValue'
        #     else
        #         echo "# did not find stack to export values ${1}"
        #     fi
        # }
        #
        # function get_cloudformation_stack_status(){
        #     aws cloudformation list-stacks | jq -r ".StackSummaries[] | select(.StackStatus != \"DELETE_COMPLETE\") | select(.StackName == \"${1}\") | .StackStatus "
        # }
        #
        # function print_variables_from_json_dictionary(){
        #     DICT=`cat ${1} | jq -r  --arg DictName "${2}" '.[$DictName]'`
        #     if [[ $DICT != null ]]; then
        #         #load any defaults from the service definition (sets env variables for subsequent rerun and CloudFormation tasks) "export \(.OutputKey)=\"${\(.OutputKey):=\(.OutputValue)}\""
        #         cat ${1} |  jq -r --arg DictName "${2}" '.[$DictName] | keys[] as $k | "export \($k)=\"${\($k):=\(.[$k] | . )}\""'
        #     fi
        # }
        #
        # function process_artifact_directory(){
        #     # Allow artifact directory to be in s3
        #     if [[ ${1} == "s3"* ]]; then
        #         TMPDIR=$(eval mktemp -d /tmp/deploy_cf.XXXXXXXXXX)
        #         ARTIFACT=$(basename ${1})
        #         aws s3 cp ${1} ${TMPDIR}
        #         pushd ${TMPDIR}/ > /dev/null
        #         unzip ${TMPDIR}/${ARTIFACT}
        #         popd > /dev/null
        #         ARTIFACT_DIRECTORY_LOCATION=${TMPDIR}/service
        #     else
        #         ARTIFACT_DIRECTORY_LOCATION=${1}
        #
        #     fi
        #     if [[ ! -d ${ARTIFACT_DIRECTORY_LOCATION} ]]; then
        #       echo "# artifact directory does not exist"
        #       return
        #     fi
        #
        #     if [[  -e ${ARTIFACT_DIRECTORY_LOCATION}/service.json ]]; then
        #         # load service definition from service.json.template
        #         export application=$( cat ${ARTIFACT_DIRECTORY_LOCATION}/service.json | jq -r '.["application"]')
        #         export ROLE=$( cat ${ARTIFACT_DIRECTORY_LOCATION}/service.json | jq -r '.["ROLE"]')
        #         export SERVICE_TYPE="${SERVICE_TYPE:=$( cat ${ARTIFACT_DIRECTORY_LOCATION}/service.json | jq -r '.["service-type"]')}"
        #         export DOCKER_REGISTRY="${DOCKER_REGISTRY:=$( cat ${ARTIFACT_DIRECTORY_LOCATION}/service.json | jq -r '.["registry-url"]')}"
        #         #allow a generic section
        #         eval $(print_variables_from_json_dictionary ${ARTIFACT_DIRECTORY_LOCATION}/service.json "deployment-parameters")
        #         #override with ENVIRONMENT specific variables
        #         eval $(print_variables_from_json_dictionary ${ARTIFACT_DIRECTORY_LOCATION}/service.json "${ENVIRONMENT}-deployment-parameters")
        #         if [[ `cat  ${ARTIFACT_DIRECTORY_LOCATION}/service.json  | jq -r --arg DictName "service-modifications" '.[$DictName]'` != null ]]; then
        #             export SERVICE_MODIFICATIONS=$(cat  ${ARTIFACT_DIRECTORY_LOCATION}/service.json  | jq -r --arg DictName "service-modifications" '.[$DictName] | join(" ")')
        #         fi
        #         #########################
        #         #for backward compatibility
        #         #use eval to allow variables to have defaults (as in evaluate export FOO="${FOO:=bar}" it does not eval without eval)
        #         eval $(print_variables_from_json_dictionary ${ARTIFACT_DIRECTORY_LOCATION}/service.json "service-parameters")
        #         #load any defaults from the service definition (sets env variables for subsequent rerun and CloudFormation tasks)
        #         eval $(print_variables_from_json_dictionary ${ARTIFACT_DIRECTORY_LOCATION}/service.json "aws-resource-parameters")
        #         #########################
        #     else
        #         echo "# service definition does not exist, exiting now!"
        #         exit 1
        #     fi
        #
        #     if [[ -e ${ARTIFACT_DIRECTORY_LOCATION}/containerurl.txt ]]; then
        #         # load the containerurl from containerurl.txt
        #         export IMAGE=$(eval cat ${ARTIFACT_DIRECTORY}/containerurl.txt)
        #     fi
        # }
        #
        # function print_export_value(){
        #     print_export_value_rec ${1} ""
        # }
        #
        # function print_export_value_rec(){
        #     if [[ "${2}" != "" ]]; then
        #         PARAMS="  --next-token ${2} "
        #     fi
        #     aws cloudformation list-exports ${PARAMS:=} > tmp.txt
        #     VALUE=$( cat tmp.txt | jq -r --arg name "${1}" '.Exports[] | select(.Name == $name ) | .Value')
        #     if [[ "${VALUE:=}" == "" && $(cat tmp.txt | jq -r '.NextToken') != "" ]]; then
        #         NEXT=$(cat tmp.txt | jq -r '.NextToken')
        #         VALUE=$(print_export_value_rec ${1} ${NEXT} )
        #     fi
        #     echo ${VALUE}
        # }
        # function generate_vpc_stack_name(){
        #     VPCAPP=$(generate_short_app_name)
        #     echo "${ENVIRONMENT}-${VPCAPP}-vpc"
        # }
        # function generate_cf_bucket_name(){
        #    VPCAPP=$(generate_short_app_name)
        #    echo "cf-templates-${VPCAPP}"
        # }
        # function generate_short_app_name(){
        #     echo $application | cut -d '-' -f1
        # }
        #
        # function generate_cluster_stack_name(){
        #    echo "${ENVIRONMENT}-${application}-cluster"
        # }
        #
        # function generate_stack_name(){
        #     echo "${ENVIRONMENT}-${application}-${ROLE}"
        # }
        #
        # function generate_resource_stack_name(){
        #     echo "${ENVIRONMENT}-${application}-${ROLE}-resources"
        # }
        #
        # function generate_modification_stack_name(){
        #     echo "${ENVIRONMENT}-${application}-${ROLE}-${1}"
        # }
        #
        # function generate_modification_resource_stack_name(){
        #     echo "${ENVIRONMENT}-${application}-${ROLE}-${1}-resources"
        # }
        #
        # function generate_key_name(){
        #     echo "${ENVIRONMENT}-${application}"
        # }
        #
        # function print_cluster_export_value(){
        #     print_export_value "$(generate_cluster_stack_name)-${1}"
        # }
        #
        # function print_ecs_service_export_value(){
        #     print_export_value "$(generate_stack_name)-${1}"
        # }
        #
        # function bucket_exists(){
        #     EXISTS=$(aws s3api list-buckets --query 'Buckets[].Name' | grep ${1})
        #     if [[ "${EXISTS:=}" == "" ]]; then
        #         echo "False"
        #     else
        #         echo "True"
        #     fi
        # }
        #
        # function key_exists(){
        #     EXISTS=$(aws ec2 describe-key-pairs --key-name ${1} 2> /dev/null )
        #     if [[ $? != 0 ]]; then
        #         echo "False"
        #     else
        #         echo "True"
        #     fi
        # }
