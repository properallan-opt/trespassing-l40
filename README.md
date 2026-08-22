# Trespassing L40 / RabbitMQ

Deploy em container do modelo de **trespassing**, seguindo o padrão operacional do `smokefire-l40`: `.diglett`, `dockerfile`, `entrypoint.sh`, `supervisord`, estágio de **oclusão** e estágio principal de inferência.

O fluxo correto é:

```text
Rabbit entrada
    |
    v
[ Oclusão ]
    |
    | câmera normal
    v
[ Trespassing ]
    |
    v
Rabbit saída

Se a oclusão detectar problema de câmera:
[ Oclusão ] ------------------------> Rabbit saída
               bypass trespassing
```

## Formato de entrada e saída

O pipeline trabalha com mensagens RabbitMQ cujo `body` contém a imagem codificada e cujos metadados ficam em `properties.headers`.

### Entrada geral do pipeline

A mensagem deve entrar na fila configurada em:

```text
nome_da_fila_entrada_oclusao
```

Formato:

```text
RabbitMQ BasicProperties
├── headers
│   ├── CameraId: <identificador único da câmera>
│   ├── AngelId: <opcional / preservado>
│   ├── TrespassingROI: <opcional>
│   └── ...demais headers do sistema
└── body: bytes da imagem codificada
```

O `body` deve conter uma imagem que possa ser decodificada pelo OpenCV com `cv2.imdecode`, normalmente JPEG.

Exemplo de headers usando a ROI cadastrada localmente pelo `CameraId`:

```json
{
  "CameraId": "CAMERA_TESTE_001",
  "AngelId": "TEST"
}
```

Exemplo de headers já no formato futuro, com a ROI enviada na própria mensagem:

```json
{
  "CameraId": "CAMERA_TESTE_001",
  "AngelId": "TEST",
  "TrespassingROI": "{\"coordinate_space\":\"normalized\",\"perimeter_polygon\":[[0.20,0.20],[0.80,0.20],[0.80,0.90],[0.20,0.90]]}"
}
```

> No RabbitMQ/Pika, `TrespassingROI` pode chegar como uma string JSON. O `ROIResolver` também aceita uma estrutura Python equivalente quando chamada internamente.

### Entrada e saída do estágio de oclusão

**Entrada**

```text
body    = imagem codificada em bytes
headers = headers originais da mensagem
```

Internamente, a imagem é decodificada para:

```text
np.ndarray
shape: (altura, largura, 3)
formato: BGR
```

A interface do detector é:

```python
class_id, class_name, confidence = detector.predict(image)
```

Tipos retornados:

```text
class_id   : int
class_name : str
confidence : float
```

A regra de roteamento é:

```text
class_id == 0
    -> câmera normal
    -> envia para nome_da_fila_entrada_trespassing

class_id != 0 e confidence >= FATOR_OCLUSAO_T4S
    -> problema de câmera
    -> bypass do trespassing
    -> envia diretamente para nome_da_fila_saida
```

**Saída**

O estágio preserva todos os headers originais e adiciona:

```text
OclusionDetection
CameraProblem
CameraProblemPercentage
OclusionClass
```

`OclusionDetection` é uma string JSON com este formato:

```json
{
  "detection": false,
  "class_id": 0,
  "class_name": "normal",
  "confidence": 0.9981
}
```

Os demais campos possuem:

```text
CameraProblem           = class_id convertido para string
CameraProblemPercentage = confidence convertido para string
OclusionClass           = nome da classe
```

Exemplo conceitual da mensagem enviada ao trespassing quando a câmera está normal:

```text
headers:
    CameraId: CAMERA_TESTE_001
    AngelId: TEST
    TrespassingROI: ...                  # se já existia na entrada
    OclusionDetection: '{...}'
    CameraProblem: '0'
    CameraProblemPercentage: '0.9981'
    OclusionClass: 'normal'

body:
    <mesmos bytes da imagem recebida>
```

Se houver problema de câmera, o formato é o mesmo, porém a mensagem vai diretamente para a fila final e não passa pelo detector de trespassing.

### Entrada e saída do estágio de trespassing

**Entrada RabbitMQ**

O trespassing consome a fila:

```text
nome_da_fila_entrada_trespassing
```

Ele recebe:

```text
body:
    bytes da imagem original

headers:
    CameraId
    headers originais
    headers adicionados pela oclusão
    TrespassingROI opcional
```

A ROI é resolvida nesta ordem:

```text
1. TrespassingROI / ROI presente nos headers
2. camera_rois.json usando CameraId
```

Se a ROI estiver em coordenadas normalizadas, o `ROIResolver` converte os pontos para **pixels** usando largura e altura da imagem antes de chamar o detector.

A interface interna do detector é:

```python
result = detector.predict(image, perimeter_polygon)
```

onde:

```text
image:
    np.ndarray BGR
    shape = (altura, largura, 3)

perimeter_polygon:
    lista de pontos em pixels
    [[x1, y1], [x2, y2], ..., [xn, yn]]
```

Exemplo:

```python
result = detector.predict(
    image,
    [
        [22.0, 630.0],
        [739.0, 633.0],
        [726.0, 149.0],
        [0.0, 154.0],
    ],
)
```

O retorno Python do `TrespassingDetector` é:

```json
{
  "detection": true,
  "bbox": [
    [100.4, 80.2, 302.7, 611.5, 0.9132, "person"]
  ],
  "max_confidence": 0.9132
}
```

Campos:

```text
detection
    true se pelo menos uma pessoa satisfizer a regra de trespassing.

bbox
    lista somente das pessoas consideradas dentro da ROI.
    Cada detecção usa:
    [x1, y1, x2, y2, confidence, class_name]
    As coordenadas da bbox são em pixels.

max_confidence
    maior confiança das detecções de pessoa retornadas pelo YOLO naquela imagem.
```

A regra de trespassing é aplicada usando o ponto inferior-central da bbox:

```text
feet_x = (x1 + x2) / 2
feet_y = y2
```

A pessoa é considerada dentro da área apenas quando esse ponto está estritamente dentro do polígono.

**Saída RabbitMQ**

O resultado do detector é serializado no header:

```text
TrespassingDetection
```

Exemplo:

```json
{
  "detection": true,
  "bbox": [
    [100.4, 80.2, 302.7, 611.5, 0.9132, "person"]
  ]
}
```

Além dele, são adicionados:

```text
TrespassingMaxConfidence
TrespassingRoiSource
TrespassingStatus
```

Exemplo de headers finais:

```text
CameraId: CAMERA_TESTE_001
OclusionDetection: '{"detection":false,"class_id":0,...}'
CameraProblem: '0'
CameraProblemPercentage: '0.9981'
OclusionClass: 'normal'
TrespassingDetection: '{"detection":true,"bbox":[[100.4,80.2,302.7,611.5,0.9132,"person"]]}'
TrespassingMaxConfidence: '0.9132'
TrespassingRoiSource: 'local_json'
TrespassingStatus: 'ok'
```

`TrespassingRoiSource` normalmente assume um destes valores:

```text
local_json
message:TrespassingROI
message:<outra-chave-de-ROI-configurada>
none
```

Se `INCLUDE_ROI_IN_OUTPUT_HEADERS=True`, também é incluído:

```text
TrespassingROIResolved
```

contendo o polígono já resolvido em **pixels**.

O `body` da mensagem final segue a regra:

```text
TrespassingDetection.detection == true
    -> body contém os bytes da imagem original

TrespassingDetection.detection == false
    -> body vazio: b""
```

Portanto, uma saída positiva é conceitualmente:

```text
headers: resultados de oclusão + resultados de trespassing
body:    imagem original
```

E uma saída negativa de trespassing:

```text
headers: resultados de oclusão + TrespassingDetection com detection=false
body:    vazio
```

### ROI ausente e mensagens de erro

Quando nenhuma ROI é encontrada e:

```text
MISSING_ROI_POLICY = "skip"
```

a mensagem segue para a fila final com:

```text
TrespassingDetection = {"detection": false, "bbox": []}
TrespassingMaxConfidence = "0.0"
TrespassingRoiSource = "none"
TrespassingStatus = "skipped_missing_roi"
body = vazio
```

Quando `MISSING_ROI_POLICY="error"`, ou quando ocorre erro de imagem/ROI/inferência, a mensagem é enviada para:

```text
nome_da_fila_erros
```

com `body` vazio e headers adicionais:

```text
Mensagem
Contexto
Error
```

### Resumo do contrato

```text
ENTRADA
    body = imagem
    headers.CameraId = câmera
    headers.TrespassingROI = ROI opcional

        |
        v

OCLUSÃO
    adiciona OclusionDetection / CameraProblem / ...

        |
        v

TRESPASSING
    resolve ROI
    detecta person
    testa bottom-center da bbox contra o polígono

        |
        v

SAÍDA
    headers.TrespassingDetection
    headers.TrespassingMaxConfidence
    headers.TrespassingRoiSource
    headers.TrespassingStatus

    body = imagem se detection=true
           vazio se detection=false
```

## Estágio 1: oclusão

Foi incluído o mesmo modelo de oclusão usado no `smokefire-l40`:

```text
modelos_prod/T4S_model_oclusion.pt
```

Arquivos:

```text
conteiner_oclusao/
├── oclusion.py
└── t4s_oclusao.py

main_oclusao.py
main_oclusao.sh
```

A regra foi mantida compatível com o smokefire:

```text
classe 0 -> câmera normal
classe != 0 e confiança >= FATOR_OCLUSAO_T4S -> problema de câmera
```

Quando existe problema de câmera, a mensagem vai diretamente para `nome_da_fila_saida`, sem executar o modelo de trespassing.

Os headers adicionados incluem:

```text
OclusionDetection
CameraProblem
CameraProblemPercentage
OclusionClass
```

Todos os headers recebidos originalmente são preservados. Portanto, uma futura `TrespassingROI` enviada pelo upstream passa pelo estágio de oclusão sem ser modificada.

## Estágio 2: trespassing

O modelo e a regra de invasão vieram do repositório `trespassing-bento`.

Ele detecta `person` e considera trespassing quando o **ponto inferior-central da bbox** está estritamente dentro do polígono da área de interesse.

O artefato atual é:

```text
modelos_prod/yolo26n_ncnn_model/
```

Por isso a configuração atual usa CPU/NCNN:

```text
DEVICE = "cpu"
```

A troca futura para `.pt`, `.onnx` ou TensorRT `.engine` fica concentrada em `MODEL_PATH` e `DEVICE`.

## Filas

Em homologação o exemplo está configurado como:

```text
gtower_Frames2IepTrespassing_frames2ocl_hml
        |
        v
gtower_Frames2IepTrespassing_ocl2trespassing_hml
        |
        v
gtower_Frames2IepTrespassing_saida_hml
```

As chaves no `.diglett` são:

```text
nome_da_fila_entrada_oclusao
nome_da_fila_entrada_trespassing
nome_da_fila_saida
nome_da_fila_erros
```

O `rabbit_test.py` publica sempre na **fila de entrada da oclusão**, e não diretamente no trespassing.

## Área de interesse / ROI

A inferência de trespassing não depende diretamente de onde a ROI veio. A classe `ROIResolver` resolve a área nesta ordem:

1. ROI presente na mensagem Rabbit;
2. JSON local relacionado ao `CameraId`.

Isso permite testar hoje usando cadastro local e, no futuro, receber a ROI diretamente do sistema upstream sem alterar a lógica do modelo.

### Cadastro local temporário

Arquivo:

```text
/config/camera_rois.json
```

Exemplo:

```json
{
  "cameras": {
    "CAMERA_TESTE_001": {
      "coordinate_space": "normalized",
      "perimeter_polygon": [
        [0.20, 0.20],
        [0.80, 0.20],
        [0.80, 0.90],
        [0.20, 0.90]
      ]
    }
  }
}
```

`coordinate_space` pode ser:

```text
normalized
pixels
```

`normalized` é recomendado porque não prende o cadastro à resolução da câmera.

O JSON é recarregado automaticamente quando o arquivo muda.

### Formato futuro no Rabbit

O recomendado é enviar no header:

```text
TrespassingROI
```

com conteúdo JSON:

```json
{
  "coordinate_space": "normalized",
  "perimeter_polygon": [
    [0.2, 0.2],
    [0.8, 0.2],
    [0.8, 0.9],
    [0.2, 0.9]
  ]
}
```

Esse header atravessa o estágio de oclusão e é consumido somente pelo trespassing.

## Supervisord

O container agora inicia **dois programas**, como no smokefire:

```text
appTrespassingOclusao
appTrespassing
```

Por padrão, o terceiro argumento do entrypoint define a quantidade dos dois processos:

```bash
trespassing hml 1
```

Também é possível controlar separadamente:

```text
NPROC_OCLUSAO
NPROC_TRESPASSING
```

## Teste local sem RabbitMQ

Testar o pipeline completo:

```bash
python3 standalone_test.py imagem.jpg \
  --camera-id CAMERA_TESTE_001 \
  --output output/trespassing.jpg
```

O fluxo executado é:

```text
imagem
  -> oclusão
  -> se câmera normal: resolve ROI
  -> trespassing
```

Para simular agora a ROI que futuramente virá do Rabbit:

```bash
python3 standalone_test.py imagem.jpg \
  --camera-id CAMERA_123 \
  --roi '{"coordinate_space":"normalized","perimeter_polygon":[[0.2,0.2],[0.8,0.2],[0.8,0.9],[0.2,0.9]]}' \
  --output output/trespassing.jpg
```

Para testar somente o modelo de trespassing, pulando o estágio de oclusão:

```bash
python3 standalone_test.py imagem.jpg \
  --camera-id CAMERA_TESTE_001 \
  --skip-occlusion
```

## Teste pelo RabbitMQ

```bash
python3 rabbit_test.py imagem.jpg \
  --camera-id CAMERA_TESTE_001
```

Com ROI enviada na mensagem:

```bash
python3 rabbit_test.py imagem.jpg \
  --camera-id CAMERA_123 \
  --roi '{"coordinate_space":"pixels","perimeter_polygon":[[100,100],[900,100],[900,650],[100,650]]}'
```

A publicação será feita em:

```text
nome_da_fila_entrada_oclusao
```

e não diretamente na fila intermediária do trespassing.

## Build

```bash
docker build -f dockerfile -t trespassing-l40:dev .
```

Execução usando a fonte empacotada no container:

```bash
docker run --rm \
  --gpus all \
  -v "$PWD/.diglett:/config/.diglett:ro" \
  -v "$PWD/config/camera_rois.json:/config/camera_rois.json:ro" \
  trespassing-l40:dev trespassing hml 1
```

## Clone do Git em runtime

O `entrypoint.sh` também permite o mesmo estilo de deploy por clone usado pelo smokefire.

```bash
docker run --rm --gpus all \
  -e DEPLOY_SOURCE=git \
  -e GIT_REPO='https://gitlab.exemplo/grupo/trespassing.git' \
  -e GIT_REF='master' \
  -e GIT_USER='...' \
  -e GIT_PASS='...' \
  -v /caminho/config:/config \
  trespassing-l40:dev trespassing prod 1
```

## Estrutura

```text
.
├── .diglett
├── dockerfile
├── entrypoint.sh
├── main_oclusao.py
├── main_oclusao.sh
├── main_trespassing.py
├── main_trespassing.sh
├── requirements.txt
├── rabbit_test.py
├── standalone_test.py
│
├── config/
│   └── camera_rois.json
│
├── conteiner_oclusao/
│   ├── __init__.py
│   ├── oclusion.py
│   └── t4s_oclusao.py
│
├── conteiner_trespassing/
│   ├── __init__.py
│   ├── detector.py
│   ├── roi_provider.py
│   └── trespassing_rabbit.py
│
├── conteiner_log/
│   └── loguru_config.py
│
└── modelos_prod/
    ├── T4S_model_oclusion.pt
    └── yolo26n_ncnn_model/
```

## ROI ausente

Em homologação:

```text
MISSING_ROI_POLICY = "skip"
```

Em produção:

```text
PROD_MISSING_ROI_POLICY = "error"
```

Assim um erro de cadastro de ROI em produção não vira falso negativo silencioso.
