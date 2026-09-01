package com.nocturne;

enum SubmissionStatus
{
	LOCAL("Captured locally"),
	SENDING("Sending to test intake…"),
	ACCEPTED("Received by test intake"),
	UNCERTAIN("Delivery unconfirmed"),
	REJECTED("Not accepted by test intake"),
	BUSY("Not sent — queue full"),
	CANCELLED("Delivery cancelled / unconfirmed");

	final String label;
	SubmissionStatus(String label) { this.label = label; }
}
