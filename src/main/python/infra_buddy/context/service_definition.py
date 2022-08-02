import json
import os

import datetime
from jsonschema import validate
from pprint import pformat
from infra_buddy.deploy.cloudformation_deploy import CloudFormationDeploy
from infra_buddy.template.template_manager import TemplateManager
from infra_buddy.utility import print_utility

_SERVICE_DEFINITION_FILE = "service.json"

_MODIFICATIONS = 'service-modifications'

_DEPLOYMENT_PARAMETERS = 'deployment-parameters'
_CI_DEPLOYMENT_PARAMETERS = 'ci-deployment-parameters'
_PROD_DEPLOYMENT_PARAMETERS = 'prod-deployment-parameters'
_DEV_DEPLOYMENT_PARAMETERS = 'dev-deployment-parameters'
_UNIT_TEST_DEPLOYMENT_PARAMETERS = 'unit-test-deployment-parameters'
_DOCKER_REGISTRY = 'registry-url'
_SERVICE_TYPE = 'service-type'
_ROLE = 'role'
_APPLICATION = 'application'


class ServiceDefinition(object):
    schema = {
        "type": "object",
        "properties": {
            _APPLICATION: {"type": "string"},
            _ROLE: {"type": "string"},
            _SERVICE_TYPE: {"type": "string"},
            _DOCKER_REGISTRY: {"type": ["string", "null"], 'maxLength': 1000},
            _DEPLOYMENT_PARAMETERS: {
                "type": "object",
                "properties": {
                },
                "additionalProperties": True
            },
            _CI_DEPLOYMENT_PARAMETERS: {
                "type": "object",
                "properties": {
                },
                "additionalProperties": True
            },
            _PROD_DEPLOYMENT_PARAMETERS: {
                "type": "object",
                "properties": {
                },
                "additionalProperties": True
            },
            _DEV_DEPLOYMENT_PARAMETERS: {
                "type": "object",
                "properties": {
                },
                "additionalProperties": True
            }
        },
        "required": [
            _APPLICATION,
            _ROLE,
            _DEPLOYMENT_PARAMETERS,
            _SERVICE_TYPE
        ]
    }

    def __init__(self, artifact_directory, environment):
        super(ServiceDefinition, self).__init__()
        self.artifact_directory = artifact_directory
        service_definition_path = os.path.join(artifact_directory, _SERVICE_DEFINITION_FILE)
        if not os.path.exists(service_definition_path):
            err_msg = f"Service definition ({_SERVICE_DEFINITION_FILE}) does not exist in artifact directory - {artifact_directory}"

            print_utility.error(err_msg)
            raise Exception(err_msg)
        with open(service_definition_path, 'r') as fp:
            service_definition = json.load(fp)
            validate(service_definition, self.schema)
            self.application = service_definition[_APPLICATION]
            self.role = service_definition[_ROLE]
            self.service_type = service_definition[_SERVICE_TYPE]
            self.docker_registry = service_definition.get(_DOCKER_REGISTRY,"")
            if _DEPLOYMENT_PARAMETERS in service_definition:
                self.deployment_parameters = service_definition[_DEPLOYMENT_PARAMETERS]
            env_deployment_parameters = '{environment}-deployment-parameters'.format(environment=environment)
            if env_deployment_parameters in service_definition:
                print_utility.info(
                    f"Updating deployment params with environment specific settings - {env_deployment_parameters}"
                )

                self.deployment_parameters.update(service_definition[env_deployment_parameters])
            print_utility.info(
                f"Loaded deployment parameters: {pformat(self.deployment_parameters, indent=4)}"
            )

            self.service_modifications = service_definition.get(_MODIFICATIONS, [])


    def generate_execution_plan(self, template_manager, deploy_ctx):
        template = template_manager.get_known_service(self.service_type)
        ret = [
            CloudFormationDeploy(
                stack_name=deploy_ctx.stack_name,
                template=template,
                deploy_ctx=deploy_ctx,
            )
        ]

        if template.has_monitor_definition():
            ret.extend(template.get_monitor_artifact().generate_execution_plan(deploy_ctx))
        if resource_deploy := template_manager.get_resource_service(
            self.artifact_directory
        ):
            ret.append(CloudFormationDeploy(stack_name=deploy_ctx.resource_stack_name,
                                            template=resource_deploy,
                                            deploy_ctx=deploy_ctx))
        else:
            print_utility.info("Addition resource template not located (aws-resources.template).")
        for mod in self.service_modifications:
            template = template_manager.get_known_service_modification(self.service_type,mod)
            ret.append(CloudFormationDeploy(stack_name=deploy_ctx.generate_modification_stack_name(mod),
                                            template=template,
                                            deploy_ctx=deploy_ctx))
            if template.has_monitor_definition():
                ret.extend(template.get_monitor_artifact().generate_execution_plan(deploy_ctx))
        return ret

    @classmethod
    def save_to_file(cls, application,role, deploy_params, service_type,known_service_modifications,destination):
        # type: (str, str, dict,str) -> str
        if destination:
            service_file_path = os.path.join(destination,_SERVICE_DEFINITION_FILE)
            readme_file_path = os.path.join(destination,"README.md")
        else:
            service_file_path = _SERVICE_DEFINITION_FILE
            readme_file_path = "README.md"
        service_definition_object = {
            _APPLICATION: application,
            _ROLE: role,
            _SERVICE_TYPE: service_type,
            _DEPLOYMENT_PARAMETERS: cls._get_params_without_default_values(deploy_params),
            _MODIFICATIONS: []}
        with open(service_file_path, 'w') as def_file:
            json.dump(service_definition_object,def_file)
        with open(readme_file_path,'w') as read:
            read.write(f"# Service Type: {service_type}\n")
            read.write(f"Generated on {datetime.datetime.now()}\n\n")
            read.write("Service template may have been modified, please verify usage with:\n")
            read.write(" ```bash\n")
            read.write(f"infra-buddy validate-template --service-type {service_type}\n")
            read.write("```\n")
            read.write("## Known Service Modifications\n")
            read.write(
                f"Define these service modifications in the service.json stanza '{_MODIFICATIONS}'\n"
            )

            read.write(" ```javascript\n")
            read.write(f"""\"{_MODIFICATIONS}\":[ \"modification-1\"]'\n""")
            read.write("```\n\n")
            read.write("| Service Modification |\n")
            read.write("| --- |\n")
            for key,val in known_service_modifications.items():
                read.write(f"| {key} |\n")
            read.write("\n")
            read.write("## Deploy parameters\n\n")
            read.write(
                f"Define these parameters in the service.json stanza '{_DEPLOYMENT_PARAMETERS}'\n\n"
            )

            read.write(
                f"For environment specific values use the corresponding stanza in the form '<environment>-{_DEPLOYMENT_PARAMETERS}'\n"
            )

            read.write("this is supported for the known environments: 'dev', 'ci', and 'prod'.\n\n")
            if len(deploy_params) > 0:
                read.write("| Parameter | Description | Default Value |\n")
                read.write("| --- | --- | --- |\n")
                for key_, definition in deploy_params.items():
                    if 'default_type' in definition and definition["default_type"] == "property":
                        parameter = definition['key']
                        description = definition.get("description","<None>")
                        default_val = definition.get("default","<None>")
                        read.write(f"| {parameter} | {description} | {default_val} |\n")
        return service_file_path

    @classmethod
    def _get_params_without_default_values(cls, deploy_params):
        ret = {}
        for key_, definition in deploy_params.items():
            if (
                'default_type' in definition
                and definition["default_type"] == "property"
                and 'default_value' not in definition
            ):
                definition_key_ = definition['key']
                ret[definition_key_] = os.environ.get(definition_key_,"")
        return ret


