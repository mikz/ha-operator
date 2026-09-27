ARG HA_BASE
FROM ${HA_BASE}
RUN apk add --no-cache iproute2
COPY tests/lab/gate.py /lab/gate.py
COPY tests/lab/configuration.yaml /lab/configuration.yaml
COPY tests/lab/custom_components/ha_operator_sim /opt/ha-operator/sim_bridge
COPY dist/ha_operator.zip /opt/ha-operator/ha_operator.zip
COPY dist/ha_operator.manifest.json /opt/ha-operator/ha_operator.manifest.json
RUN python3 -m zipfile -e /opt/ha-operator/ha_operator.zip /opt/ha-operator/integration
