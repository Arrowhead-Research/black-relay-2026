FROM eclipse-temurin:17-jammy
RUN apt update && \
    apt-get install emacs-nox net-tools netcat vim -y
ENV COUNTRY_CODE='US'
ENV CITY='North Augusta'
ENV STATE='South Carolina'
ENV ORGANIZATION='blackrelay'
ENV ORGANIZATIONAL_UNIT='NA'
ENV USER_COUNT=
ENV ADMINS_COUNT=
ENV ENROLLMENT_TYPE=automated_enrollment
ENV AUTOMATED_ADMIN_USERNAME=admin
ENV AUTOMATED_ADMIN_PASSWORD='ChangeMe-Admin1!'
ENV AUTOMATED_CLIENT_USERNAME=user
ENV AUTOMATED_CLIENT_PASSWORD='ChangeMe-User123!'
ENV SERVER_NAME=
ENV IP_OR_DOMAIN_NAME=

RUN useradd -g 0 -u 1001 tak \
    && usermod -a -G root tak \
    && mkdir /opt/configurator

USER tak:0

COPY --chown=tak:0 /tak/ /opt/tak
COPY --chown=tak:0 configurator.sh pki-init.sh /opt/configurator/
COPY --chown=tak:0 CoreConfig.xml /opt/tak
COPY --chown=tak:0 tak-server-configurator-template.pref /opt/tak/certs
COPY --chown=tak:0 tak-server-configurator-manifest.xml /opt/tak/certs

RUN chmod u=rwx /opt/tak \
	&& find /opt/tak -type f -name "*.sh" -exec chmod u=rwx {} \; \
	&& find /opt/configurator -type f -name "*.sh" -exec chmod u=rwx {} \;


ENTRYPOINT ["/bin/bash", "-c", "/opt/tak/configureInDocker.sh init"]
