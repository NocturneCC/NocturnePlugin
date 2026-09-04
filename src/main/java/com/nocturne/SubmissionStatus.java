package com.nocturne;

enum SubmissionStatus
{
	LOCAL("Captured locally"),
	UNPRICED("Not accepted — price unavailable"),
	INELIGIBLE("Not accepted — below 500,000 gp per unit"),
	SENDING("Sending to test intake…"),
	ACCEPTED("Received by test intake"),
	UNCERTAIN("Delivery unconfirmed"),
	REJECTED("Not accepted by test intake"),
	BUSY("Not sent — queue full"),
	CANCELLED("Delivery cancelled / unconfirmed");

	final String label;
	SubmissionStatus(String label) { this.label = label; }
}
