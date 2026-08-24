#!/usr/bin/env bash
set -Eeuo pipefail

APPLICATION="${1:-}"
ENVIRONMENT="${2:-}"
NPROC="${3:-}"

usage() {
    echo "Uso: $0 <trespassing> <prod|hml> <nprocs>"
}

if [[ -z "${APPLICATION}" || -z "${ENVIRONMENT}" || -z "${NPROC}" ]]; then
    usage
    exit 1
fi

if [[ "${APPLICATION}" != "trespassing" ]]; then
    echo "Erro: APPLICATION deve ser 'trespassing'. Recebido: ${APPLICATION}" >&2
    exit 1
fi

if [[ "${ENVIRONMENT}" != "prod" && "${ENVIRONMENT}" != "hml" ]]; then
    echo "Erro: ENVIRONMENT deve ser 'prod' ou 'hml'. Recebido: ${ENVIRONMENT}" >&2
    exit 1
fi


# ================================================================================================
# PARAMETROS
# ================================================================================================

export APP_ENVIRONMENT="${ENVIRONMENT}"

NPROC_TRESPASSING="${NPROC_TRESPASSING:-$NPROC}"
NPROC_OCLUSAO="${NPROC_OCLUSAO:-$NPROC}"

TRESPASSING_CMD="${TRESPASSING_CMD:-bash main_trespassing.sh}"
OCLUSAO_CMD="${OCLUSAO_CMD:-bash main_oclusao.sh}"

APP_DIR="${APP_DIR:-/app/source}"

DEPLOY_SOURCE="${DEPLOY_SOURCE:-local}"
BUNDLED_SOURCE="${BUNDLED_SOURCE:-/opt/trespassing-source}"

GIT_REPO="${GIT_REPO:-}"
GIT_REF="${GIT_REF:-master}"
GIT_USER="${GIT_USER:-}"
GIT_PASS="${GIT_PASS:-}"
GIT_SSL_NO_VERIFY="${GIT_SSL_NO_VERIFY:-0}"


# ================================================================================================
# FUNCOES
# ================================================================================================

build_clone_url() {
    local repo="${GIT_REPO}"

    #
    # Se GIT_USER/GIT_PASS foram fornecidos, assumimos autenticação HTTP(S).
    #
    # O repositório pode vir no formato:
    #
    #   git@gitlab.exemplo.com:grupo/repositorio.git
    #
    # e será convertido para:
    #
    #   https://gitlab.exemplo.com/grupo/repositorio.git
    #
    # As credenciais NÃO entram na URL. Elas serão fornecidas via GIT_ASKPASS.
    #
    if [[ -n "${GIT_USER}" || -n "${GIT_PASS}" ]]; then
        if [[ -z "${GIT_USER}" || -z "${GIT_PASS}" ]]; then
            echo "Erro: GIT_USER e GIT_PASS devem ser definidos juntos." >&2
            return 1
        fi

        if [[ "${repo}" =~ ^git@([^:]+):(.+)$ ]]; then
            repo="https://${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"

        elif [[ "${repo}" =~ ^ssh://git@([^/]+)/(.+)$ ]]; then
            repo="https://${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"

        elif [[ "${repo}" == http://* || "${repo}" == https://* ]]; then
            :
        else
            repo="https://${repo}"
        fi

        printf '%s\n' "${repo}"
        return 0
    fi

    #
    # Sem GIT_USER/GIT_PASS, preservamos SSH/HTTPS normalmente.
    #
    if [[ "${repo}" == git@*:* ||
          "${repo}" == ssh://* ||
          "${repo}" == http://* ||
          "${repo}" == https://* ]]; then
        printf '%s\n' "${repo}"
    else
        printf 'https://%s\n' "${repo}"
    fi
}


clone_repository() {
    local clone_url
    clone_url="$(build_clone_url)"

    echo "Clonando repositório..."
    echo "GIT_REPO=${GIT_REPO}"
    echo "GIT_REF=${GIT_REF}"
    echo "CLONE_URL=${clone_url}"

    if [[ "${GIT_SSL_NO_VERIFY}" == "1" ]]; then
        export GIT_SSL_NO_VERIFY=1
    fi

    #
    # Deploy token / usuário+senha HTTP(S)
    #
    if [[ -n "${GIT_USER}" && -n "${GIT_PASS}" ]]; then
        local askpass
        askpass="$(mktemp)"

        cat > "${askpass}" <<'ASKPASS'
#!/usr/bin/env bash

case "$1" in
    *Username*)
        printf '%s\n' "${GIT_USER}"
        ;;
    *Password*)
        printf '%s\n' "${GIT_PASS}"
        ;;
    *)
        printf '\n'
        ;;
esac
ASKPASS

        chmod 700 "${askpass}"

        export GIT_USER
        export GIT_PASS

        GIT_ASKPASS="${askpass}" \
        GIT_TERMINAL_PROMPT=0 \
        git clone \
            --branch "${GIT_REF}" \
            "${clone_url}" \
            "${APP_DIR}"

        rm -f "${askpass}"

    else
        #
        # Sem usuário/senha: SSH, credential helper, etc.
        #
        git clone \
            --branch "${GIT_REF}" \
            "${clone_url}" \
            "${APP_DIR}"
    fi
}


prepare_source() {
    rm -rf "${APP_DIR}"
    mkdir -p "$(dirname "${APP_DIR}")"

    if [[ "${DEPLOY_SOURCE}" == "git" ]]; then
        if [[ -z "${GIT_REPO}" ]]; then
            echo "Erro: DEPLOY_SOURCE=git exige GIT_REPO." >&2
            exit 1
        fi

        clone_repository

    elif [[ "${DEPLOY_SOURCE}" == "local" ]]; then
        echo "Usando fonte empacotada no container."

        if [[ ! -d "${BUNDLED_SOURCE}" ]]; then
            echo "Erro: BUNDLED_SOURCE não existe: ${BUNDLED_SOURCE}" >&2
            exit 1
        fi

        cp -a "${BUNDLED_SOURCE}" "${APP_DIR}"

    else
        echo "Erro: DEPLOY_SOURCE deve ser 'git' ou 'local'. Recebido: ${DEPLOY_SOURCE}" >&2
        exit 1
    fi
}


prepare_config() {
    mkdir -p \
        /config \
        /app/logs \
        /var/log/supervisord

    if [[ ! -f /config/.diglett ]]; then
        if [[ ! -f "${APP_DIR}/.diglett" ]]; then
            echo "Erro: ${APP_DIR}/.diglett não encontrado." >&2
            exit 1
        fi

        cp "${APP_DIR}/.diglett" /config/.diglett

        echo "Criado /config/.diglett a partir do padrão do repositório."
    fi

    if [[ ! -f /config/camera_rois.json ]]; then
        if [[ ! -f "${APP_DIR}/config/camera_rois.json" ]]; then
            echo "Erro: ${APP_DIR}/config/camera_rois.json não encontrado." >&2
            exit 1
        fi

        cp \
            "${APP_DIR}/config/camera_rois.json" \
            /config/camera_rois.json

        echo "Criado /config/camera_rois.json de exemplo."
    fi
}


prepare_supervisor() {
    cat > /etc/supervisor/conf.d/trespassing.conf <<SUPERVISOR
[supervisord]
user=root
childlogdir=/var/log/supervisord/
logfile=/var/log/supervisord/supervisord.log
logfile_maxbytes=50MB
logfile_backups=10
loglevel=info
pidfile=/var/run/supervisord.pid
nodaemon=true


[program:appTrespassingOclusao]
process_name=%(program_name)s_%(process_num)02d
directory=${APP_DIR}
command=${OCLUSAO_CMD}
numprocs=${NPROC_OCLUSAO}
autostart=true
autorestart=true
startsecs=2
stopasgroup=true
killasgroup=true
redirect_stderr=true
stdout_logfile=/var/log/supervisord/oclusao_%(process_num)02d.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=10
environment=APP_ENVIRONMENT="${ENVIRONMENT}",DIGLETT_PATH="/config/.diglett"


[program:appTrespassing]
process_name=%(program_name)s_%(process_num)02d
directory=${APP_DIR}
command=${TRESPASSING_CMD}
numprocs=${NPROC_TRESPASSING}
autostart=true
autorestart=true
startsecs=2
stopasgroup=true
killasgroup=true
redirect_stderr=true
stdout_logfile=/var/log/supervisord/trespassing_%(process_num)02d.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=10
environment=APP_ENVIRONMENT="${ENVIRONMENT}",DIGLETT_PATH="/config/.diglett"
SUPERVISOR
}


# ================================================================================================
# MAIN
# ================================================================================================

prepare_source
prepare_config
prepare_supervisor

echo
echo "Iniciando pipeline trespassing"
echo "  environment=${ENVIRONMENT}"
echo "  oclusao=${NPROC_OCLUSAO}"
echo "  trespassing=${NPROC_TRESPASSING}"
echo "  source=${DEPLOY_SOURCE}"
echo "  app=${APP_DIR}"
echo

exec /usr/bin/supervisord \
    -c /etc/supervisor/conf.d/trespassing.conf
