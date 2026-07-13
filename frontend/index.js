const dscc = window.dscc;

function drawViz(data) {

	document.body.innerHTML = `
		<div id="chat-container">

			<div id="messages"></div>

			<div id="input-area">
				<input
					type="text"
					id="question"
					placeholder="Pregunta algo..."
				/>

				<button id="sendBtn">
					Enviar
				</button>
			</div>

		</div>
	`;

	document
		.getElementById("sendBtn")
		.addEventListener("click", sendQuestion);

	document
		.getElementById("question")
		.addEventListener("keydown", function(e){

			if(e.key === "Enter"){
				sendQuestion();
			}
		});

	async function sendQuestion(){

		const input =
			document.getElementById("question");

		const question =
			input.value.trim();

		if(!question){
			return;
		}

		const messages =
			document.getElementById("messages");

		messages.innerHTML += `
			<div class="message user">
				<div class="bubble">
					${escapeHtml(question)}
				</div>
			</div>
		`;

		input.value = "";

		messages.innerHTML += `
			<div class="message bot" id="loading">
				<div class="bubble">
					Analizando...
				</div>
			</div>
		`;

		messages.scrollTop =
			messages.scrollHeight;

		try{

			// CONTEXTO DEL DASHBOARD
			const table = data.tables.DEFAULT;

			const fields =
				table.headers.map(h => ({
					id: h.id,
						name: h.name,
						type: h.type
					}));

			const payload = {
				question: question,
				schema: fields
			};

			const response = await fetch(
				"http://opsa-production.eba-de4umm9g.us-east-1.elasticbeanstalk.com/chat",
				{
					method:"POST",
					headers:{
						"Content-Type":"application/json"
					},
					body:JSON.stringify(payload)
				}
			);

			const result =
				await response.json();

			document
				.getElementById("loading")
				.remove();

			messages.innerHTML += `
				<div class="message bot">
					<div class="bubble">
						${escapeHtml(result.answer)}
					</div>
				</div>
			`;

		}catch(error){

			document
				.getElementById("loading")
				.remove();

			messages.innerHTML += `
				<div class="message bot">
					<div class="bubble">
						Error conectando API
					</div>
				</div>
			`;
		}

		messages.scrollTop =
			messages.scrollHeight;
	}
}

function escapeHtml(text){

	return text
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;");
}

dscc.subscribeToData(drawViz, {transform: dscc.objectTransform});
