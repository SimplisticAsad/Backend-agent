# ac.ticket.closed_read_only  (requirements: requirement.ticket_management; operations: operation.ticket.update)
Feature: Closed tickets are read-only (negative case)

  Scenario: Closed tickets are read-only (negative case)
    Given the user is signed in
    When an agent edits a closed ticket
    Then Closed tickets cannot be changed (error TICKET_CLOSED)
    And no data is changed
